"""Golf-schoon R17: pin `_run_producer` and `build_terminal_snapshot`."""

from __future__ import annotations

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
