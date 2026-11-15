"""Characterisation of ``spawn_claude`` before and after the R38b extract-method (golf schoon).

Only the boundary to the external ``claude`` process is patched: ``SubprocessAdapter``, the one
object in ``claude_spawn`` that starts ``claude -p``. Every test pins observable behaviour (the
deliver kwargs, the order of side effects, log lines, return values and exception types) so the
refactor must keep them byte for byte.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from provider_spawns import claude_spawn
from worker_health_monitor import HealthStatus, SLOW_THRESHOLD


@dataclass
class _Event:
    type: str
    data: Any


class _FakeAdapter:  # stands in for SubprocessAdapter; records every call in ``log``
    def __init__(self, log: List[Any], events: List[Any], *, rc: Optional[int] = 0,
                 deliver_ok: bool = True, stop_raises: bool = False,
                 rc_cache: Optional[Dict[str, int]] = None) -> None:
        self.log, self.events, self.rc = log, events, rc
        self.deliver_ok, self.stop_raises = deliver_ok, stop_raises
        self.event_store = object()
        if rc_cache is not None:
            self._returncode_cache = rc_cache

    def deliver(self, terminal_id, dispatch_id, **kwargs):
        self.log.append(("deliver", terminal_id, dispatch_id, kwargs))
        return NS(success=self.deliver_ok, failure_reason=None if self.deliver_ok else "unknown role")

    def read_events_with_timeout(self, terminal_id, *, chunk_timeout, total_deadline):
        self.log.append(("read", terminal_id, chunk_timeout, total_deadline))
        for item in self.events:
            if isinstance(item, BaseException):
                raise item
            yield item

    def stop(self, terminal_id):
        self.log.append(("stop", terminal_id))
        if self.stop_raises:
            raise RuntimeError("stop broke")

    def observe(self, terminal_id):
        self.log.append(("observe", terminal_id))
        return NS(transport_state={"returncode": self.rc})

    def get_session_id(self, terminal_id):
        self.log.append(("session", terminal_id))
        return "sess-1"

    def was_timed_out(self, terminal_id):
        self.log.append(("timed_out", terminal_id))
        return False

    def deadline_exceeded_seconds(self, terminal_id):
        self.log.append(("deadline", terminal_id))
        return None


class _Health:
    def __init__(self, log: List[Any], status: HealthStatus = HealthStatus.ACTIVE) -> None:
        self.log, self.status, self._event_store = log, status, None

    def update(self, event):
        self.log.append(("health", event.type))

    def health_status(self):
        self.log.append(("health_status",))
        return self

    def log_stuck_event(self):
        self.log.append(("stuck",))


EVENTS = [
    _Event("init", {"model": " claude-opus-5-5 "}),
    _Event("init", {"model": "second-init-ignored"}),
    _Event("text", {"text": "hel"}),
    _Event("text", None),
    _Event("text", {"text": "lo"}),
    _Event("result", {"text": "fallback", "usage": {"input_tokens": 3}}),
]


@pytest.fixture
def run(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_BASE_URL", "VNX_CHUNK_TIMEOUT", "VNX_TOTAL_DEADLINE",
                 claude_spawn.provider_switch.PROJECT_ENV):
        monkeypatch.delenv(name, raising=False)

    def _run(events=(), adapter_kwargs=None, **spawn_kwargs):
        log: List[Any] = []
        adapter = _FakeAdapter(log, list(events), **(adapter_kwargs or {}))
        with patch.object(claude_spawn, "SubprocessAdapter", lambda: adapter):
            result = claude_spawn.spawn_claude(
                "do it", "opus", "d-1", "T1", scrub_env_keys=frozenset(), cwd=tmp_path,
                **spawn_kwargs,
            )
        return result, log, adapter
    return _run


def test_happy_path_deliver_kwargs_call_order_and_result(run, tmp_path):
    def on_spawn(pid):
        return None

    result, log, adapter = run(
        EVENTS, extra_env={"A": "1"}, extra_cli_args=["--x"], resume_session="s0", role="r",
        requires_mcp=True, scope_args=["--s"], on_spawn=on_spawn, unknown_kwarg=1,
    )
    assert log[0] == ("deliver", "T1", "d-1", {
        "instruction": "do it", "model": "opus", "cwd": tmp_path, "resume_session": "s0",
        "extra_env": {"A": "1"}, "extra_cli_args": ["--x"], "role": "r", "requires_mcp": True,
        "scrub_env_keys": frozenset(), "scope_args": ["--s"], "on_spawn": on_spawn,
    })
    assert log[1:] == [("read", "T1", 300.0, 900.0), ("observe", "T1"), ("session", "T1"),
                       ("timed_out", "T1"), ("deadline", "T1")]
    assert (result.returncode, result.events_written, result.session_id) == (0, 6, "sess-1")
    assert result.completion == {"text": "fallback", "usage": {"input_tokens": 3}}
    assert result.token_usage == {"input_tokens": 3}
    assert (result.completion_text, result.model_resolved) == ("hello", "claude-opus-5-5")
    assert (result.timed_out, result.stopped_early, result.error) == (False, False, None)
    assert result._adapter is adapter and result.spawn_refused is None


def test_completion_text_falls_back_to_result_text_and_empty_usage_is_none(run):
    result, _, _ = run([_Event("result", {"text": "only", "usage": {}}), _Event("init", "x")])
    assert (result.completion_text, result.token_usage, result.model_resolved) == ("only", None, None)


@pytest.mark.parametrize("chunk,total,expected", [
    ("5", "7.5", (5.0, 7.5)), ("bad", None, (300.0, 900.0)),
])
def test_env_timeout_overrides(run, monkeypatch, chunk, total, expected):
    monkeypatch.setenv("VNX_CHUNK_TIMEOUT", chunk)
    if total is not None:
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", total)
    _, log, _ = run()
    assert log[1] == ("read", "T1") + expected


@pytest.mark.parametrize("rc,cache,expected", [
    (None, {"T1": 4}, 4), (None, {"T2": 4}, 1), (None, None, 1), (0, {"T1": 4}, 0), (3, None, 3),
])
def test_returncode_fallback(run, rc, cache, expected):
    result, _, _ = run(adapter_kwargs={"rc": rc, "rc_cache": cache})
    assert result.returncode == expected


def test_deliver_refused(run, caplog):
    with caplog.at_level(logging.WARNING, logger=claude_spawn.logger.name):
        result, log, adapter = run(EVENTS, adapter_kwargs={"deliver_ok": False})
    assert [entry[0] for entry in log] == ["deliver"]
    assert (result.returncode, result.events_written, result.session_id) == (1, 0, None)
    assert result.spawn_refused == "deliver_failed: unknown role" and result._adapter is adapter
    assert "SubprocessAdapter.deliver() failed for T1 — unknown role" in caplog.text


@pytest.mark.parametrize("stop_raises", [False, True])
def test_on_event_false_stops_the_stream(run, caplog, stop_raises):
    seen: List[str] = []

    def on_event(event):
        seen.append(event.type)
        return False if event.type == "text" else None

    with caplog.at_level(logging.DEBUG, logger=claude_spawn.logger.name):
        result, log, _ = run(EVENTS, adapter_kwargs={"stop_raises": stop_raises}, on_event=on_event)
    assert seen == ["init", "init", "text"]
    assert result.stopped_early is True and result.events_written == 3
    assert result.completion_text == "hel" and result.returncode == 0
    assert log[2:4] == [("stop", "T1"), ("observe", "T1")]
    assert ("adapter.stop() after on_event=False failed: stop broke" in caplog.text) is stop_raises


def test_event_writer_health_on_event_order_and_raising_writer(run, caplog):
    log_ref: List[Any] = []

    def writer(terminal_id, payload, *, dispatch_id):
        log_ref.append(("writer", terminal_id, payload, dispatch_id))
        raise ValueError("writer broke")

    health = _Health(log_ref)
    with caplog.at_level(logging.DEBUG, logger=claude_spawn.logger.name):
        result, _, adapter = run(
            [_Event("text", {"text": "a"})], event_writer=writer, health_monitor=health,
            on_event=lambda event: log_ref.append(("on_event", event.type)),
        )
    assert health._event_store is adapter.event_store
    assert log_ref == [("health", "text"), ("health_status",),
                       ("writer", "T1", {"type": "text", "data": {"text": "a"}}, "d-1"),
                       ("on_event", "text")]
    assert "event_writer raised: writer broke" in caplog.text and result.returncode == 0


def test_health_keeps_an_existing_event_store(run):
    health = _Health([])
    health._event_store = existing = object()
    run(health_monitor=health)
    assert health._event_store is existing


def test_stuck_logging_is_throttled(run):
    log: List[Any] = []
    health = _Health(log, HealthStatus.STUCK)
    clock = iter([SLOW_THRESHOLD, SLOW_THRESHOLD + 1, 2 * SLOW_THRESHOLD + 5])
    with patch.object(claude_spawn.time, "monotonic", lambda: next(clock)):
        run([_Event("text", {"text": str(i)}) for i in range(3)], health_monitor=health)
    assert log.count(("stuck",)) == 2 and log.count(("health_status",)) == 2


@pytest.mark.parametrize("rc,expected", [(None, 1), (0, 1), (9, 9)])
@pytest.mark.parametrize("stop_raises", [False, True])
def test_stream_failure_fails_closed(run, caplog, rc, expected, stop_raises):
    events = [_Event("init", {"model": "m"}), _Event("text", {"text": "x"}),
              subprocess.SubprocessError("pipe gone")]
    with caplog.at_level(logging.DEBUG, logger=claude_spawn.logger.name):
        result, log, _ = run(events, adapter_kwargs={"rc": rc, "stop_raises": stop_raises})
    assert [entry[0] for entry in log[1:]] == ["read", "stop", "observe", "session"]
    assert (result.returncode, result.events_written, result.error) == (expected, 2, "pipe gone")
    assert (result.completion, result.completion_text, result.model_resolved) == ({}, "x", "m")
    assert (result.timed_out, result.stopped_early, result._adapter) == (False, False, None)
    assert "stream read failure for dispatch=d-1 terminal=T1: pipe gone" in caplog.text
    assert ("adapter.stop() in error handler raised: stop broke" in caplog.text) is stop_raises


@pytest.mark.parametrize("exc", [json.JSONDecodeError("bad", "x", 0), OSError("os")])
def test_other_caught_stream_errors(run, exc):
    result, _, _ = run([exc])
    assert result.returncode == 1 and result.error == str(exc)


def test_uncaught_exception_propagates(run):
    with pytest.raises(RuntimeError, match="boom"):
        run([_Event("text", {"text": "a"}), RuntimeError("boom")])


def test_invalid_base_url_family_raises_before_deliver(run):
    with pytest.raises(ValueError, match="is not a switchable family"):
        run(base_url_family="not-a-family")
