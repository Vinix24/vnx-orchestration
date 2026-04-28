"""Tests for scripts/lib/state_rebuild_trigger.py — shared throttle/rebuild helper."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import state_rebuild_trigger


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Route state_rebuild_trigger state resolution to a tmp dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    return state_dir


# ---------------------------------------------------------------------------
# Test 1: fires Popen when throttle expired
# ---------------------------------------------------------------------------


def test_maybe_trigger_fires_popen_when_stale(isolated_state, monkeypatch):
    """maybe_trigger_state_rebuild calls Popen when throttle is expired."""
    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: isolated_state)

    throttle = isolated_state / ".last_state_rebuild_ts"
    throttle.write_text("1000", encoding="utf-8")  # very old timestamp

    popen_calls: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)

    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", _FakePopen)

    result = state_rebuild_trigger.maybe_trigger_state_rebuild()

    assert result is True
    assert len(popen_calls) == 1
    assert "build_t0_state.py" in popen_calls[0][-1]


# ---------------------------------------------------------------------------
# Test 2: respects throttle — no Popen when recent
# ---------------------------------------------------------------------------


def test_maybe_trigger_respects_throttle(isolated_state, monkeypatch):
    """maybe_trigger_state_rebuild must NOT call Popen when throttle is fresh."""
    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: isolated_state)

    throttle = isolated_state / ".last_state_rebuild_ts"
    throttle.write_text(str(int(time.time())), encoding="utf-8")  # just updated

    popen_calls: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)

    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", _FakePopen)

    result = state_rebuild_trigger.maybe_trigger_state_rebuild()

    assert result is False
    assert len(popen_calls) == 0


# ---------------------------------------------------------------------------
# Test 3: fires Popen when no throttle file exists
# ---------------------------------------------------------------------------


def test_maybe_trigger_fires_when_no_throttle_file(isolated_state, monkeypatch):
    """maybe_trigger_state_rebuild must call Popen when no throttle file exists yet."""
    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: isolated_state)

    popen_calls: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)

    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", _FakePopen)

    result = state_rebuild_trigger.maybe_trigger_state_rebuild()

    assert result is True
    assert len(popen_calls) == 1


# ---------------------------------------------------------------------------
# Test 4: throttle marker is integer after write
# ---------------------------------------------------------------------------


def test_throttle_marker_written_as_integer(isolated_state, monkeypatch):
    """Throttle file must contain an integer epoch string (no decimal point)."""
    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: isolated_state)

    throttle = isolated_state / ".last_state_rebuild_ts"
    throttle.write_text("1000", encoding="utf-8")  # stale

    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", lambda cmd, **kw: None)

    state_rebuild_trigger.maybe_trigger_state_rebuild()

    written = throttle.read_text(encoding="utf-8").strip()
    assert "." not in written, f"Throttle must be integer, got: {written!r}"
    assert written.isdigit(), f"Throttle must be digits only, got: {written!r}"


# ---------------------------------------------------------------------------
# Test 5: returns False on any Popen exception (best-effort)
# ---------------------------------------------------------------------------


def test_maybe_trigger_returns_false_on_popen_exception(isolated_state, monkeypatch):
    """maybe_trigger_state_rebuild must not propagate exceptions — return False."""
    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: isolated_state)

    throttle = isolated_state / ".last_state_rebuild_ts"
    throttle.write_text("1000", encoding="utf-8")  # stale

    def _bad_popen(*a, **kw):
        raise OSError("binary not found")

    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", _bad_popen)

    result = state_rebuild_trigger.maybe_trigger_state_rebuild()

    assert result is False
