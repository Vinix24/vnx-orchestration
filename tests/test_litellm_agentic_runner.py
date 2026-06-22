"""Tests for the agentic litellm runner (tool-use loop + sandboxing)."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

ADAPTERS = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "adapters"
sys.path.insert(0, str(ADAPTERS))

import _litellm_agentic_runner as ar  # noqa: E402


# --------------------------------------------------------------------------- #
# Tool layer — sandboxing and behaviour
# --------------------------------------------------------------------------- #

def test_write_then_read_roundtrip(tmp_path):
    assert ar._tool_write_file(tmp_path, {"path": "a/b.txt", "content": "hello"}).startswith("wrote")
    assert (tmp_path / "a" / "b.txt").read_text() == "hello"
    assert ar._tool_read_file(tmp_path, {"path": "a/b.txt"}) == "hello"


def test_read_missing_file_is_error(tmp_path):
    out = ar._tool_read_file(tmp_path, {"path": "nope.txt"})
    assert out.startswith("ERROR")


def test_list_dir(tmp_path):
    (tmp_path / "x.txt").write_text("1")
    (tmp_path / "sub").mkdir()
    listing = ar._tool_list_dir(tmp_path, {"path": "."})
    assert "x.txt" in listing
    assert "sub/" in listing


def test_run_command_in_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("1")
    out = ar._tool_run_command(tmp_path, {"command": "ls"}, timeout=30)
    assert "exit_code=0" in out
    assert "marker.txt" in out


def test_run_command_timeout(tmp_path):
    out = ar._tool_run_command(tmp_path, {"command": "sleep 5"}, timeout=1)
    assert "timed out" in out


@pytest.mark.parametrize("escape", ["../outside.txt", "../../etc/passwd", "/etc/passwd"])
def test_path_traversal_refused(tmp_path, escape):
    with pytest.raises(ValueError):
        ar._safe_path(tmp_path, escape)


def test_write_traversal_surfaces_as_error(tmp_path):
    # _execute_tool wraps the ValueError into an is_error result, never escapes cwd.
    result, is_error = ar._execute_tool("write_file", {"path": "../evil.txt", "content": "x"}, tmp_path, 30)
    assert is_error
    assert not (tmp_path.parent / "evil.txt").exists()


def test_unknown_tool_is_error(tmp_path):
    result, is_error = ar._execute_tool("frobnicate", {}, tmp_path, 30)
    assert is_error
    assert "unknown tool" in result


def test_parse_tool_arguments_handles_garbage():
    assert ar._parse_tool_arguments('{"a": 1}') == {"a": 1}
    assert ar._parse_tool_arguments("not json") == {}
    assert ar._parse_tool_arguments(None) == {}
    assert ar._parse_tool_arguments("[1,2]") == {}  # non-dict JSON


def test_accumulate_usage():
    totals = {"prompt_tokens": 0, "completion_tokens": 0}
    ar._accumulate_usage({"prompt_tokens": 10, "completion_tokens": 4}, totals)
    ar._accumulate_usage({"prompt_tokens": 5, "completion_tokens": 1}, totals)
    assert totals == {"prompt_tokens": 15, "completion_tokens": 5}


@pytest.mark.parametrize("msg,etype", [
    ("401 Unauthorized: bad api key", "credentials_missing"),
    ("connection refused", "service_unavailable"),
    ("model produced nonsense", "completion_error"),
])
def test_classify_error(msg, etype):
    assert ar._classify_error(msg)[0] == etype


# --------------------------------------------------------------------------- #
# Full loop — fake litellm, no network
# --------------------------------------------------------------------------- #

def _fake_message(content=None, tool_calls=None):
    return types.SimpleNamespace(content=content, tool_calls=tool_calls)


def _fake_tool_call(call_id, name, arguments):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def _fake_response(message, finish_reason, usage):
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=usage)


def _run_main_with_fake(monkeypatch, payload, responses):
    """Inject a fake litellm whose completion() yields `responses` in order."""
    calls = {"i": 0}

    def fake_completion(**kwargs):
        r = responses[calls["i"]]
        calls["i"] += 1
        return r

    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = fake_completion
    fake_litellm.suppress_debug_info = False
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = ar.main()
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return rc, events


def test_full_loop_writes_file_and_completes(monkeypatch, tmp_path):
    responses = [
        _fake_response(
            _fake_message(tool_calls=[_fake_tool_call("c1", "write_file",
                          json.dumps({"path": "out.txt", "content": "DONE"}))]),
            finish_reason="tool_calls",
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        ),
        _fake_response(
            _fake_message(content="All set."),
            finish_reason="stop",
            usage={"prompt_tokens": 130, "completion_tokens": 5},
        ),
    ]
    payload = {"model": "openrouter/z-ai/glm-5.2", "prompt": "write out.txt",
               "cwd": str(tmp_path), "max_turns": 5}
    rc, events = _run_main_with_fake(monkeypatch, payload, responses)

    assert rc == 0
    assert (tmp_path / "out.txt").read_text() == "DONE"

    etypes = [e.get("event_type") for e in events]
    assert "tool_use" in etypes
    assert "tool_result" in etypes
    assert "usage_complete" in etypes
    assert "complete" in etypes

    usage_evt = next(e for e in events if e.get("event_type") == "usage_complete")
    assert usage_evt["usage"] == {"prompt_tokens": 230, "completion_tokens": 25}
    complete_evt = next(e for e in events if e.get("event_type") == "complete")
    assert complete_evt["stop_reason"] == "stop"
    assert complete_evt["turns"] == 2


def test_missing_cwd_errors(monkeypatch):
    payload = {"model": "openrouter/z-ai/glm-5.2", "prompt": "x", "cwd": "/no/such/dir/here"}
    rc, events = _run_main_with_fake(monkeypatch, payload, [])
    assert rc == ar._EXIT_ERR
    assert any(e.get("error_type") == "runner_error" for e in events)


def test_missing_key_errors(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = lambda **k: None
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"model": "openrouter/z-ai/glm-5.2", "prompt": "x", "cwd": str(tmp_path)})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = ar.main()
    assert rc == ar._EXIT_CREDS
