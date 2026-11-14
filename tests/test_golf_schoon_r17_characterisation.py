"""Golf-schoon R17: pin `_run_producer`, runner `main` and `build_terminal_snapshot`."""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import queue
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from canonical_event import CanonicalEvent
import _streaming_drainer as sd
import canonical_state_views as views

_spec = importlib.util.spec_from_file_location(
    "r17_litellm_agentic_runner", LIB / "adapters" / "_litellm_agentic_runner.py")
ar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ar)


def _norm(raw):
    return CanonicalEvent("d1", "T1", "codex", raw["type"], raw)


def _produce(script, tmp_path, chunk_timeout=30.0, total_deadline=60.0, tee=None):
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            start_new_session=True)
    q, done, timed = queue.Queue(), threading.Event(), threading.Event()
    calls = []
    store = types.SimpleNamespace(append=lambda t, e, dispatch_id: calls.append((t, dispatch_id, e.event_type)))
    sd._run_producer(process=proc, terminal_id="T1", dispatch_id="d1", event_store=store,
                     chunk_timeout=chunk_timeout, total_deadline=total_deadline,
                     result_queue=q, seen_complete=done, timed_out=timed,
                     normalize_fn=_norm, provider_name="codex", raw_tee_path=tee)
    proc.wait(timeout=10)
    return list(q.queue), calls, done.is_set(), timed.is_set()


def test_producer_parses_lines_tees_and_flags_complete(tmp_path):
    out = '{"type": "text"}\\n\\nnot json\\n{"type": "complete"}\\n'
    tee = tmp_path / "sub" / "raw.bin"
    events, calls, done, timed = _produce(
        f"import sys; sys.stdout.write('{out}')", tmp_path, tee=tee)
    assert [e.event_type for e in events] == ["text", "error", "complete"]
    assert all(e.observability_tier == 1 for e in events)
    assert events[1].data["raw"] == "not json"
    assert calls == [("T1", "d1", "text"), ("T1", "d1", "error"), ("T1", "d1", "complete")]
    assert (done, timed) == (True, False)
    assert tee.read_bytes() == b'{"type": "text"}\n\nnot json\n{"type": "complete"}\n'


def test_producer_tee_open_failure_logs_and_keeps_draining(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with caplog.at_level(logging.WARNING, logger=sd.logger.name):
        events, _, done, _ = _produce("print('{\"type\": \"complete\"}')", tmp_path,
                                      tee=blocker / "raw.bin")
    assert [e.event_type for e in events] == ["complete"] and done
    assert "raw tee open failed" in caplog.text


@pytest.mark.parametrize("chunk,deadline,reason,marker,log", [
    (1.0, 30.0, "chunk timeout 1s exceeded", {}, "_streaming_drainer: chunk timeout (1s) for T1"),
    (5.0, 1.0, "total deadline reached during final stall window (1s remaining, configured "
     "chunk_timeout=5s not fully elapsed)", {"deadline_exceeded": True, "deadline_seconds": 1.0},
     "_streaming_drainer: deadline-compressed stall (1s) for T1"),
    (30.0, 0.0, "total deadline 0s exceeded", {"deadline_exceeded": True, "deadline_seconds": 0.0},
     "_streaming_drainer: total deadline (0s) exceeded for T1"),
])
def test_producer_stall_and_deadline_kill(tmp_path, caplog, chunk, deadline, reason, marker, log):
    with caplog.at_level(logging.WARNING, logger=sd.logger.name):
        events, calls, done, timed = _produce("import time; time.sleep(30)", tmp_path,
                                              chunk_timeout=chunk, total_deadline=deadline)
    assert [e.data for e in events] == [{"reason": reason, **marker}]
    assert calls == [("T1", "d1", "error")] and (done, timed) == (False, True)
    assert log in caplog.text


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


def _reconcile(calls, terminals, tmux=None, reasons=("r1",)):
    def fake(state_root, *, config, repair):
        calls.append((config.stale_after_seconds, config.receipt_scan_lines,
                      config.active_window_seconds, config.allow_tmux_probe, repair))
        return {"terminals": terminals, "evidence": {"tmux": tmux or {}},
                "degraded": True, "degraded_reasons": list(reasons)}
    return fake


def test_snapshot_reconciler_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(views, "reconcile_terminal_state", _reconcile(calls, {
        "T1": {"status": "busy", "claimed_by": "d-9", "last_activity": "2026-01-01T00:00:00Z",
               "reconciled_source": "receipts"},
        "T2": {"status": "working"}}, tmux={"T1": {"command": "node"}}))
    (tmp_path / "context_window_T1.json").write_text('{"remaining_pct": 10, "ts": 0}')
    snap = views.build_terminal_snapshot(tmp_path, receipt_scan_lines=7, active_window_seconds=9,
                                         allow_tmux_probe=False)
    assert calls == [(180, 7, 9, False, False)]
    assert (snap["source"], snap["primary_state_ok"], snap["primary_error"]) == (
        "reconciler", False, "primary_missing")
    assert snap["degraded"] is True and snap["degraded_reasons"] == ["primary_missing", "r1"]
    t1, t2, t3 = (snap["terminals"][t] for t in ("T1", "T2", "T3"))
    assert (t1["status"], t1["current_task"], t1["current_command"], t1["record_source"]) == (
        "working", "d-9", "node", "receipts")
    assert t1["attention"]["type"] == "stale" and t1["context_usage_pct"] == 90
    assert (t2["status"], t2["current_task"], t2["last_update"]) == ("idle", None, "never")
    assert (t3["status"], t3["current_command"], t3["needs_human"]) == ("unknown", "claude", False)
    assert list(snap) == ["generated_at", "source", "primary_state_ok", "primary_error",
                          "primary_age_seconds", "degraded", "degraded_reasons", "terminals"]


def _rec(t, s, la):
    return {"terminal_id": t, "status": s, "claimed_by": None, "claimed_at": None,
            "lease_expires_at": None, "last_activity": la, "version": 1}


def test_snapshot_primary_with_stale_override(tmp_path, monkeypatch):
    now = views._now_iso()
    doc = {"schema_version": 1, "terminals": {
        "T1": _rec("T1", "idle", now), "T2": _rec("T2", "idle", "2026-01-01T00:00:00Z"),
        "T3": _rec("T3", "weird", "2026-01-01T00:00:00Z")}}
    (tmp_path / "terminal_state.json").write_text(json.dumps(doc))
    calls = []
    monkeypatch.setattr(views, "reconcile_terminal_state", _reconcile(calls, {
        "T2": {"status": "working", "claimed_by": "d-2", "last_activity": now},
        "T3": {"status": "idle", "last_activity": now}}))
    snap = views.build_terminal_snapshot(tmp_path)
    assert calls == [(180, 600, 240, True, False)]
    assert (snap["source"], snap["primary_state_ok"], snap["primary_error"]) == (
        "terminal_state", True, None)
    assert snap["degraded_reasons"] == ["r1", "terminal_state_per_terminal_stale"]
    t1, t2, t3 = (snap["terminals"][t] for t in ("T1", "T2", "T3"))
    assert (t1["status"], t1["record_source"], t1["source"]) == ("idle", None, "terminal_state")
    assert (t2["status"], t2["current_task"], t2["record_source"]) == ("working", "d-2", "reconciler")
    assert (t3["status"], t3["record_source"]) == ("idle", "reconciler")
