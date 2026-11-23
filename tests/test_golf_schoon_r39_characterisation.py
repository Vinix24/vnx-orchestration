"""Characterisation of spawn_kimi, normalize_kimi_event and spawn_litellm_agentic
(golf schoon R39). Written green on the code before the extract-method, kept green after.
Only the external processes are faked; the heartbeat tee lives in a temp dir."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from provider_spawns import kimi_spawn as ks  # noqa: E402
from provider_spawns import litellm_spawn as ls  # noqa: E402

_ST = {"cache_creation_tokens": 0, "cache_read_tokens": 0}
_NORMALIZE_CASES = [
    ({"status": 403, "message": "x"}, "error", {"reason": "quota_or_auth", "message": "[quota_or_auth] provider=kimi http_status=403 raw={'status': 403, 'message': 'x'}"}),
    ({"code": "zz", "msg": "quota exceeded"}, "error", {"reason": "quota_or_auth", "message": "[quota_or_auth] provider=kimi http_status=0 raw={'code': 'zz', 'msg': 'quota exceeded'}"}),
    ({"role": "tool", "content": [{"type": "think", "think": "r"}], "tool_call_id": 7}, "tool_result", {"tool_use_id": "7", "content": "r"}),
    ({"role": "assistant", "content": [{"type": "think", "text": "r"}, {"type": "text", "text": "a"}, "x"], "tool_calls": [1]}, "text", {"text": "a", "reasoning": "r", "tool_calls": [1]}),
    ({"role": "assistant", "content": []}, "text", {"text": ""}),
    ({"role": "user", "content": [{"type": "text", "text": "p"}]}, "info", {"raw_type": "role:user", "raw": "{'role': 'user', 'content': [{'type': 'text', 'text': 'p'}]}"}),
    ({"role": "assistant", "content": "plain"}, "text", {"text": "plain"}),
    ({"event_type": "assistant_text", "content": 5}, "text", {"text": "5"}),
    ({"type": "text", "content": "t"}, "text", {"text": "t"}),
    ({"event_type": "tool_call", "name": "n", "id": 3}, "tool_use", {"name": "n", "input": {}, "id": "3"}),
    ({"event_type": "tool_result", "tool_call_id": "c", "output": "o"}, "tool_result", {"tool_use_id": "c", "content": "o"}),
    ({"event_type": "usage_complete", "usage": {"prompt_tokens": 2, "completion_tokens": None}}, "text", {"text": "", "token_count": {"input_tokens": 2, "output_tokens": 0, **_ST}}),
    ({"event_type": "complete"}, "complete", {}),
    ({"event_type": "error", "error": "boom"}, "error", {"message": "boom"}),
    ({"event_type": "error"}, "error", {"message": "{'event_type': 'error'}"}),
    ({"event_type": "TurnBegin"}, "text", {"text": ""}),
    ({"event_type": "StepBegin"}, "text", {"text": ""}),
    ({"event_type": "ContentPart", "text": "c"}, "text", {"text": "c"}),
    ({"event_type": "ThinkPart", "content": "th"}, "thinking", {"text": "th"}),
    ({"event_type": "TextPart", "content": "tp"}, "text", {"text": "tp"}),
    ({"event_type": "StatusUpdate", "usage": {"prompt_tokens": 4, "output_tokens": 5, "cache_read_tokens": 6}}, "text", {"text": "", "token_count": {"input_tokens": 4, "output_tokens": 5, "cache_creation_tokens": 0, "cache_read_tokens": 6}}),
    ({"event_type": "TurnEnd"}, "complete", {}),
    ({"event_type": "Weird", "k": 1}, "info", {"raw_type": "Weird", "raw": "{'event_type': 'Weird', 'k': 1}"}),
    ({}, "info", {"raw_type": "", "raw": "{}"}),
]


@pytest.mark.parametrize("raw,etype,data", _NORMALIZE_CASES)
def test_normalize_kimi_event_shape(raw, etype, data):
    ev = ks.normalize_kimi_event(raw, "T9", "d-9")
    assert (ev.event_type, ev.data, ev.provider, ev.terminal_id, ev.dispatch_id, ev.observability_tier) == (
        etype, data, "kimi", "T9", "d-9", 1)


def _kimi_proc(events, rc=0, stderr=""):
    data = b"".join((json.dumps(e) + "\n").encode() for e in events)
    r, w = os.pipe()
    t = threading.Thread(target=lambda: (os.write(w, data), os.close(w)), daemon=True)
    t.start()
    proc = MagicMock(returncode=rc)
    proc.poll.return_value = rc
    proc.wait = MagicMock(return_value=rc)
    proc.stdout = os.fdopen(r, "rb", buffering=0)
    proc.stderr = io.BytesIO(stderr.encode())
    proc._t = t
    return proc


_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_ANSWER = [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]


def _run_kimi(tmp_path, events=_ANSWER, rc=0, stderr="", monitor=None, **kw):
    proc = _kimi_proc(events, rc, stderr)
    seen = {}
    real_consume = ks._consume_kimi_stream

    def _consume(**kwargs):
        seen.update(chunk_timeout=kwargs["chunk_timeout"], total_deadline=kwargs["total_deadline"])
        return real_consume(**kwargs)

    with patch.object(ks, "_start_kimi_subprocess", return_value=(proc, None)) as start, \
            patch.object(ks, "_resolve_heartbeat_log_path", return_value=tmp_path / "hb.log"), \
            patch.object(ks, "_heartbeat_monitor_loop", monitor or (lambda *a: None)), \
            patch.object(ks, "_consume_kimi_stream", _consume), \
            patch.object(ks, "_harvest_session_token_usage", return_value={"input_tokens": 1}) as harvest, \
            patch.object(ks, "_reap_kimi_session") as reap:
        res = ks.spawn_kimi("the prompt", model="kimi-x", dispatch_id="d-r39", terminal_id="T2", **kw)
    proc._t.join(timeout=5)
    return res, seen, start, harvest, reap


def test_spawn_kimi_both_sinks_raise_value_error():
    with pytest.raises(ValueError, match="either event_store OR event_writer"):
        ks.spawn_kimi("p", event_writer=lambda *a, **k: None, event_store=MagicMock())


def test_spawn_kimi_clean_run_argv_event_harvest_and_reap(tmp_path, caplog):
    written = []
    with caplog.at_level(logging.INFO, logger=ks.logger.name):
        res, seen, start, harvest, reap = _run_kimi(
            tmp_path, stderr=f"To resume this session: kimi -r {_SID}\n",
            chunk_timeout=7.0, total_deadline=60.0, cwd=tmp_path,
            event_writer=lambda tid, ev, dispatch_id=None: written.append((tid, ev, dispatch_id)))
    assert (res.returncode, res.completion_text, res.error) == (0, "done", None)
    assert res.token_usage == {"input_tokens": 1}
    assert seen == {"chunk_timeout": ks.coerce_chunk_stall(7.0, 60.0), "total_deadline": 60.0}
    first = written[0]
    assert first[0] == "T2" and first[2] == "d-r39" and first[1]["event_type"] == "info"
    assert first[1]["data"]["kind"] == "effective_argv" and first[1]["data"]["yolo"] is True
    assert first[1]["data"]["work_dir"] == str(tmp_path)
    assert "<prompt:10chars>" in first[1]["data"]["argv"]
    assert any(r.getMessage().startswith("kimi_spawn: launching kimi -p <10 chars> -m kimi-x effective_argv=")
               for r in caplog.records)
    env = start.call_args[0][1]
    assert harvest.call_args[0] == (_SID, env, str(tmp_path))
    reap.assert_called_once_with(_SID, env)


def test_spawn_kimi_scales_the_stall_with_the_deadline(tmp_path):
    res, seen, _s, harvest, reap = _run_kimi(tmp_path, chunk_timeout=5.0, total_deadline=50.0)
    assert seen == {"chunk_timeout": ks.coerce_chunk_stall(5.0, 50.0), "total_deadline": 50.0}
    assert res.returncode == 0 and res.token_usage is None
    harvest.assert_not_called()
    reap.assert_not_called()


def test_spawn_kimi_heartbeat_kill_rewrites_error_and_rc(tmp_path):
    def _monitor(proc, path, did, tid, model, stop, killed):
        assert (path, did, tid, model) == (tmp_path / "hb.log", "d-r39", "T2", "kimi-x")
        killed.set()

    res, _seen, _s, harvest, _r = _run_kimi(tmp_path, monitor=_monitor,
                                            stderr=f"kimi -r {_SID}\n")
    assert res.returncode == 1
    assert res.error == ("worker heartbeat: killed due to silence exceeding threshold "
                         "(dispatch=d-r39); see unified_reports/d-r39.md")
    harvest.assert_not_called()


def test_spawn_kimi_start_failure_returns_err_result_and_removes_config(tmp_path):
    err = ks.KimiSpawnResult(returncode=127, completion_text="", events_written=0,
                             session_id=None, timed_out=False, error="kimi not found")
    paths = []
    real_write = ks._write_kimi_config_file

    def _write():
        paths.append(real_write())
        return paths[-1]

    with patch.object(ks, "_write_kimi_config_file", _write), \
            patch.object(ks, "_start_kimi_subprocess", return_value=(None, err)):
        assert ks.spawn_kimi("p", dispatch_id="d-r39") is err
    assert not Path(paths[0]).exists()


class _LProc:
    def __init__(self, out=b"", timeout=False, rc=0):
        self.out, self.timeout, self.returncode, self.calls = out, timeout, rc, []

    def communicate(self, input=None, timeout=None):
        self.calls.append(("communicate", input, timeout))
        if self.timeout and timeout is not None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.out, b""

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        return self.returncode

    def kill(self):
        self.calls.append(("kill",))


def _run_agentic(tmp_path, proc=None, popen_exc=None, writer=None):
    seen = {}

    real_popen = subprocess.Popen

    def _popen(cmd, **kw):
        if "_litellm_agentic_runner.py" not in str(cmd):
            return real_popen(cmd, **kw)
        seen.update(cmd=cmd, **kw)
        if popen_exc:
            raise popen_exc
        return proc

    with patch("provider_spawns.litellm_spawn.subprocess.Popen", _popen):
        res = ls.spawn_litellm_agentic("pr", "anthropic/claude-sonnet-4-6", "d-a", "T1", lane="L",
                                       cwd=tmp_path, event_writer=writer, total_deadline=9.0)
    return res, seen


def test_agentic_parses_events_in_order(tmp_path):
    out = b"\n".join([b'{"event_type":"text","content":"he"}', b"junk", b"",
                      b'{"event_type":"usage_complete","usage":{"prompt_tokens":3,"completion_tokens":null}}',
                      b'{"event_type":"tool_use","x":1}', b'{"content":"llo","event_type":"text"}'])
    proc, written = _LProc(out), []
    res, seen = _run_agentic(tmp_path, proc, writer=lambda t, e, dispatch_id=None: written.append(e))
    assert json.loads(proc.calls[0][1])["cwd"] == str(tmp_path) and proc.calls[0][2] == 9.0
    assert seen["cwd"] == str(tmp_path) and seen["start_new_session"] is True
    assert (res.returncode, res.completion_text, res.events_written, res.error) == (0, "hello", 4, None)
    assert res.token_usage == {"prompt_tokens": 3, "completion_tokens": 0}
    assert [(e["event_type"], e["data"]) for e in written] == [
        ("text", {"content": "he"}), ("usage_complete", {"usage": {"prompt_tokens": 3, "completion_tokens": None}}),
        ("tool_use", {"x": 1}), ("text", {"content": "llo"})]
    assert written[0]["provider_meta"] == {"lane": "L"}


def test_agentic_error_event_forces_rc1_and_writer_failures_counted(tmp_path, caplog):
    def _boom(*a, **k):
        raise RuntimeError("w")

    proc = _LProc(b'{"error_type":"E","message":"m"}\n{"event_type":"text","content":"x"}')
    with caplog.at_level(logging.ERROR, logger=ls.logger.name):
        res, _ = _run_agentic(tmp_path, proc, writer=_boom)
    assert (res.returncode, res.error, res.event_writer_failures, res.stopped_early) == (1, "m", 2, False)
    assert "spawn_litellm_agentic: event_writer callback failed (dispatch=d-a): w" in caplog.text


def test_agentic_timeout_kills_then_drains(tmp_path):
    proc = _LProc(b'{"event_type":"text","content":"p"}', timeout=True)
    res, _ = _run_agentic(tmp_path, proc)
    assert res.timed_out is True and res.completion_text == "p"
    assert [c[0] for c in proc.calls] == ["communicate", "kill", "communicate", "wait"]


@pytest.mark.parametrize("exc,rc,prefix", [(FileNotFoundError("nf"), 127, "agentic runner not found: nf"),
                                           (OSError("os"), 126, "failed to spawn agentic runner: os")])
def test_agentic_popen_failures(tmp_path, exc, rc, prefix):
    res, _ = _run_agentic(tmp_path, popen_exc=exc)
    assert (res.returncode, res.error, res.events_written, res.timed_out) == (rc, prefix, 0, False)
