"""Golf-schoon R17b: pin the litellm agentic runner's `main`."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

_spec = importlib.util.spec_from_file_location(
    "r17_litellm_agentic_runner", LIB / "adapters" / "_litellm_agentic_runner.py")
ar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ar)


def _resp(content=None, calls=None, finish="stop"):
    msg = types.SimpleNamespace(content=content, tool_calls=calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason=finish)],
                                 usage={"prompt_tokens": 1, "completion_tokens": 2})


def _run_main(monkeypatch, payload, script):
    seen = []

    def completion(**kwargs):
        seen.append([m["role"] for m in kwargs["messages"]])
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=completion))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(ar.time, "sleep", lambda s: seen.append(("sleep", s)))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = ar.main()
    return rc, [json.loads(line) for line in out.getvalue().splitlines()], seen


def test_main_retry_tool_call_nudge_and_complete(monkeypatch, tmp_path):
    call = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(
        name="write_file", arguments=json.dumps({"path": "a.txt", "content": "hi"})))
    script = [RuntimeError("429 rate limit"), _resp("w", [call], "tool_calls"),
              _resp("done"), _resp("done")]
    payload = {"model": "openrouter/x", "prompt": "p", "cwd": str(tmp_path), "max_turns": 3}
    rc, events, seen = _run_main(monkeypatch, payload, script)
    assert rc == 0 and (tmp_path / "a.txt").read_text() == "hi"
    assert events == [
        {"event_type": "retry", "attempt": 1, "delay_s": 2.0, "message": "429 rate limit"},
        {"event_type": "text", "content": "w"},
        {"event_type": "tool_use", "name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}},
        {"event_type": "tool_result", "name": "write_file", "is_error": False,
         "content": "wrote 2 bytes to a.txt"},
        {"event_type": "text", "content": "done"},
        {"event_type": "nudge", "remaining": 1},
        {"event_type": "text", "content": "done"},
        {"event_type": "usage_complete", "usage": {"prompt_tokens": 3, "completion_tokens": 6}},
        {"event_type": "complete", "turns": 3, "stop_reason": "stop"},
    ]
    assert seen[1] == ("sleep", 2.0)
    assert seen[3] == ["system", "user", "assistant", "tool"]


def test_main_fatal_and_exhausted_errors(monkeypatch, tmp_path):
    payload = {"model": "openrouter/x", "prompt": "p", "cwd": str(tmp_path)}
    rc, events, _ = _run_main(monkeypatch, payload, [RuntimeError("401 unauthorized")])
    assert (rc, events) == (1, [{"error_type": "credentials_missing", "message": "401 unauthorized"}])
    script = [RuntimeError("503 overloaded")] * (ar._MAX_API_RETRIES + 1)
    rc, events, seen = _run_main(monkeypatch, payload, script)
    assert rc == 2 and [e.get("attempt") for e in events[:-1]] == [1, 2, 3, 4]
    assert events[-1] == {"error_type": "completion_error", "message": "503 overloaded"}
    assert [s[1] for s in seen if s[0] == "sleep"] == [2.0, 4.0, 8.0, 16.0]


@pytest.mark.parametrize("payload,message", [
    ({"prompt": "p"}, "model field required"),
    ({"model": "m"}, "prompt field required"),
    ({"model": "m", "prompt": "p", "cwd": "/no/such/dir"}, "cwd must be an existing directory: '/no/such/dir'"),
])
def test_main_rejects_an_unusable_request(monkeypatch, payload, message):
    rc, events, _ = _run_main(monkeypatch, payload, [])
    assert (rc, events) == (ar._EXIT_ERR, [{"error_type": "runner_error", "message": message}])
