"""Tests for dispatch_register.py — path resolution and shared-lock contract.

Covers:
  14. Path resolution: VNX_STATE_DIR env var is respected
  15. Canonical resolver uses resolve_paths()['VNX_STATE_DIR']
  16. Canonical resolver: VNX_STATE_DIR overrides VNX_DATA_DIR+EXPLICIT
  17. Canonical resolver: fallback uses VNX_DATA_DIR/state when EXPLICIT=1
  18. Fallback ignores VNX_DATA_DIR when VNX_DATA_DIR_EXPLICIT not set
  19. Fallback honors VNX_DATA_DIR when VNX_DATA_DIR_EXPLICIT=1
  20. Fallback honors VNX_STATE_DIR (BLOCKING fix Codex PR #277)
  21. Fallback: VNX_STATE_DIR beats VNX_DATA_DIR+EXPLICIT=1
  22. read_events takes shared lock (blocks on concurrent exclusive writer)
"""

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_register
from dispatch_register import append_event, read_events


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _reg_path(data_dir: Path) -> Path:
    return data_dir / "state" / "dispatch_register.ndjson"


@pytest.fixture()
def isolated_data_dir(monkeypatch, tmp_path):
    """Route all register I/O into a fresh tmp dir for tests that request it."""
    data_dir = tmp_path / ".vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _run_exclusive_write_then_read(reg: Path, record: dict):
    """Write record under LOCK_EX; concurrent reader collects results after lock releases."""
    import fcntl as _fcntl
    import time

    writer_has_lock = threading.Event()
    reader_results: list = []
    errors: list = []

    def locked_writer():
        try:
            with reg.open("a", encoding="utf-8") as wh:
                _fcntl.flock(wh.fileno(), _fcntl.LOCK_EX)
                writer_has_lock.set()
                time.sleep(0.05)  # hold lock long enough for reader to block
                wh.write(json.dumps(record) + "\n")
        except Exception as exc:
            errors.append(exc)

    def locked_reader():
        writer_has_lock.wait()
        reader_results.extend(read_events())

    w = threading.Thread(target=locked_writer)
    r = threading.Thread(target=locked_reader)
    w.start()
    r.start()
    w.join(timeout=2)
    r.join(timeout=2)
    return reader_results, errors


# ---------------------------------------------------------------------------
# 14. VNX_STATE_DIR path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_register_lands_in_vnx_state_dir(self, tmp_path, monkeypatch):
        custom_state = tmp_path / "custom-state"
        monkeypatch.setenv("VNX_STATE_DIR", str(custom_state))
        result = append_event("dispatch_created", dispatch_id="path-test")
        assert result is True
        expected = custom_state / "dispatch_register.ndjson"
        assert expected.exists(), f"Register not found at {expected}"
        rec = json.loads(expected.read_text().strip())
        assert rec["dispatch_id"] == "path-test"


# ---------------------------------------------------------------------------
# 15–17. Canonical resolver: VNX_STATE_DIR override, fallback
# ---------------------------------------------------------------------------

class TestPathResolutionCanonical:
    def test_register_path_uses_canonical_resolver(self, tmp_path, monkeypatch):
        """Canonical resolver is used: register lands at resolve_paths()['VNX_STATE_DIR']."""
        custom_state = tmp_path / "canonical-state"
        monkeypatch.setenv("VNX_STATE_DIR", str(custom_state))
        result = append_event("dispatch_created", dispatch_id="canonical-test")
        assert result is True
        expected = custom_state / "dispatch_register.ndjson"
        assert expected.exists(), f"Register not at VNX_STATE_DIR: {expected}"
        rec = json.loads(expected.read_text().strip())
        assert rec["dispatch_id"] == "canonical-test"

    def test_register_path_respects_state_dir_override(self, tmp_path, monkeypatch):
        """VNX_STATE_DIR=X lands register at X, not VNX_DATA_DIR/state."""
        custom_data = tmp_path / "override-data"
        custom_state = tmp_path / "override-state"
        monkeypatch.setenv("VNX_DATA_DIR", str(custom_data))
        monkeypatch.setenv("VNX_STATE_DIR", str(custom_state))
        result = append_event("dispatch_created", dispatch_id="override-test")
        assert result is True
        expected = custom_state / "dispatch_register.ndjson"
        assert expected.exists(), f"Register not at VNX_STATE_DIR override: {expected}"
        wrong = custom_data / "state" / "dispatch_register.ndjson"
        assert not wrong.exists(), f"Register incorrectly landed at VNX_DATA_DIR/state: {wrong}"

    def test_register_path_fallback(self, tmp_path, monkeypatch):
        """When vnx_paths import fails and VNX_DATA_DIR_EXPLICIT=1, falls back to VNX_DATA_DIR/state."""
        custom_data = tmp_path / "fallback-data"
        monkeypatch.setenv("VNX_DATA_DIR", str(custom_data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)
        with patch.dict(sys.modules, {"vnx_paths": None}):
            result = append_event("dispatch_created", dispatch_id="fallback-test")
        assert result is True
        expected = custom_data / "state" / "dispatch_register.ndjson"
        assert expected.exists(), f"Fallback register not found at {expected}"
        rec = json.loads(expected.read_text().strip())
        assert rec["dispatch_id"] == "fallback-test"


# ---------------------------------------------------------------------------
# 18–19. Fallback VNX_DATA_DIR_EXPLICIT contract
# ---------------------------------------------------------------------------

class TestFallbackExplicitFlag:
    def test_fallback_ignores_vnx_data_dir_when_not_explicit(self, tmp_path, monkeypatch):
        """Fallback uses repo-relative .vnx-data when VNX_DATA_DIR_EXPLICIT is absent."""
        custom_data = tmp_path / "no-explicit"
        monkeypatch.setenv("VNX_DATA_DIR", str(custom_data))
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)
        with patch.dict(sys.modules, {"vnx_paths": None}):
            path = dispatch_register._register_path()
        # Must NOT route to custom_data — EXPLICIT is not set
        assert str(custom_data) not in str(path), (
            f"Fallback incorrectly honored VNX_DATA_DIR without EXPLICIT=1: {path}"
        )
        assert path.name == "dispatch_register.ndjson"
        assert "state" in path.parts

    def test_fallback_honors_vnx_data_dir_when_explicit(self, tmp_path, monkeypatch):
        """Fallback routes to VNX_DATA_DIR/state when VNX_DATA_DIR_EXPLICIT=1."""
        custom_data = tmp_path / "with-explicit"
        monkeypatch.setenv("VNX_DATA_DIR", str(custom_data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)
        with patch.dict(sys.modules, {"vnx_paths": None}):
            path = dispatch_register._register_path()
        assert path == custom_data / "state" / "dispatch_register.ndjson", (
            f"Fallback did not honor VNX_DATA_DIR with EXPLICIT=1: {path}"
        )


# ---------------------------------------------------------------------------
# 22. read_events shared-lock: reader blocks behind active exclusive writer
# ---------------------------------------------------------------------------

class TestReadEventsSharedLock:
    def test_read_events_takes_shared_lock(self, isolated_data_dir):
        """Reader blocks on LOCK_EX held by writer and observes the complete record."""
        reg = _reg_path(isolated_data_dir)
        reg.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": "2026-01-01T00:00:00.000000Z",
            "event": "dispatch_created",
            "dispatch_id": "lock-test",
        }
        reader_results, errors = _run_exclusive_write_then_read(reg, record)
        assert not errors, f"Writer thread raised: {errors}"
        assert len(reader_results) == 1, f"Expected 1 event, got {len(reader_results)}: {reader_results}"
        assert reader_results[0]["dispatch_id"] == "lock-test"


# ---------------------------------------------------------------------------
# 20–21. Fallback honors VNX_STATE_DIR (BLOCKING fix — Codex PR #277 round 3)
# ---------------------------------------------------------------------------

class TestFallbackStateDir:
    def test_fallback_honors_vnx_state_dir(self, tmp_path, monkeypatch):
        """When vnx_paths import fails, VNX_STATE_DIR is used as state dir (not ignored)."""
        custom_state = tmp_path / "state-from-env"
        monkeypatch.setenv("VNX_STATE_DIR", str(custom_state))
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        with patch.dict(sys.modules, {"vnx_paths": None}):
            path = dispatch_register._register_path()
        assert path == custom_state / "dispatch_register.ndjson", (
            f"Fallback did not honor VNX_STATE_DIR: {path}"
        )

    def test_fallback_precedence_state_dir_over_data_dir_explicit(self, tmp_path, monkeypatch):
        """VNX_STATE_DIR beats VNX_DATA_DIR+EXPLICIT=1 in the fallback chain."""
        custom_state = tmp_path / "state-wins"
        custom_data = tmp_path / "data-loses"
        monkeypatch.setenv("VNX_STATE_DIR", str(custom_state))
        monkeypatch.setenv("VNX_DATA_DIR", str(custom_data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        with patch.dict(sys.modules, {"vnx_paths": None}):
            path = dispatch_register._register_path()
        assert path == custom_state / "dispatch_register.ndjson", (
            f"VNX_STATE_DIR did not win over VNX_DATA_DIR+EXPLICIT: {path}"
        )
        assert str(custom_data) not in str(path)
