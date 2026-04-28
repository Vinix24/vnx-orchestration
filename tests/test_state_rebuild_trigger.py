"""Tests for state_rebuild_trigger.py shared helper module.

15 test cases covering throttle behavior, marker contract, env precedence,
Popen failure handling, atomic write, and CLI entry.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
LIB_DIR = REPO_ROOT / "scripts" / "lib"

sys.path.insert(0, str(LIB_DIR))

import state_rebuild_trigger as srt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_popen():
    """Return a mock that looks like a successful Popen result."""
    return mock.MagicMock()


# ---------------------------------------------------------------------------
# Test 1: First call (no throttle file) → fires Popen, writes marker
# ---------------------------------------------------------------------------

def test_first_call_fires_popen(tmp_path: Path) -> None:
    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()) as mock_p:
        result = srt.maybe_trigger_state_rebuild()

    assert result is True
    mock_p.assert_called_once()
    throttle = tmp_path / ".last_state_rebuild_ts"
    assert throttle.exists()


# ---------------------------------------------------------------------------
# Test 2: Second call within throttle window → does NOT fire, returns False
# ---------------------------------------------------------------------------

def test_second_call_within_window_is_throttled(tmp_path: Path) -> None:
    throttle = tmp_path / ".last_state_rebuild_ts"
    throttle.write_text(str(int(time.time())), encoding="utf-8")

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen") as mock_p:
        result = srt.maybe_trigger_state_rebuild(throttle_seconds=30)

    assert result is False
    mock_p.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Second call after throttle expires → fires Popen
# ---------------------------------------------------------------------------

def test_call_after_throttle_expires_fires(tmp_path: Path) -> None:
    throttle = tmp_path / ".last_state_rebuild_ts"
    old_ts = int(time.time()) - 35
    throttle.write_text(str(old_ts), encoding="utf-8")

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()) as mock_p:
        result = srt.maybe_trigger_state_rebuild(throttle_seconds=30)

    assert result is True
    mock_p.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4: Throttle marker NOT written when Popen raises
# ---------------------------------------------------------------------------

def test_popen_failure_does_not_write_marker(tmp_path: Path) -> None:
    throttle = tmp_path / ".last_state_rebuild_ts"

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", side_effect=OSError("popen failed")):
        result = srt.maybe_trigger_state_rebuild()

    assert result is False
    assert not throttle.exists(), "marker must not be written when Popen raises"


# ---------------------------------------------------------------------------
# Test 5: Float-encoded marker tolerated
# ---------------------------------------------------------------------------

def test_float_marker_tolerated(tmp_path: Path) -> None:
    throttle = tmp_path / ".last_state_rebuild_ts"
    throttle.write_text("12345.6", encoding="utf-8")

    # 12345 is ancient, so rebuild should fire
    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()) as mock_p:
        result = srt.maybe_trigger_state_rebuild(throttle_seconds=30)

    assert result is True
    mock_p.assert_called_once()


# ---------------------------------------------------------------------------
# Test 6: Empty marker file → treats as 0, fires
# ---------------------------------------------------------------------------

def test_empty_marker_treated_as_zero(tmp_path: Path) -> None:
    throttle = tmp_path / ".last_state_rebuild_ts"
    throttle.write_text("", encoding="utf-8")

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()) as mock_p:
        result = srt.maybe_trigger_state_rebuild()

    assert result is True
    mock_p.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: Corrupted marker file (non-numeric) → treats as 0, fires
# ---------------------------------------------------------------------------

def test_corrupted_marker_treated_as_zero(tmp_path: Path) -> None:
    throttle = tmp_path / ".last_state_rebuild_ts"
    throttle.write_text("not-a-number!!", encoding="utf-8")

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()) as mock_p:
        result = srt.maybe_trigger_state_rebuild()

    assert result is True
    mock_p.assert_called_once()


# ---------------------------------------------------------------------------
# Test 8: VNX_STATE_DIR override → marker lands at VNX_STATE_DIR/.last_state_rebuild_ts
# ---------------------------------------------------------------------------

def test_vnx_state_dir_override(tmp_path: Path) -> None:
    custom_state = tmp_path / "custom_state"
    custom_state.mkdir()

    with mock.patch("vnx_paths.resolve_paths", return_value={"VNX_STATE_DIR": str(custom_state)}):
        result_dir = srt._resolve_state_dir()

    assert result_dir == custom_state


# ---------------------------------------------------------------------------
# Test 9: VNX_DATA_DIR + EXPLICIT=1 → marker at VNX_DATA_DIR/state/
# ---------------------------------------------------------------------------

def test_vnx_data_dir_explicit(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with mock.patch("vnx_paths.resolve_paths", return_value={"VNX_STATE_DIR": str(data_dir / "state")}):
        result_dir = srt._resolve_state_dir()

    assert result_dir == data_dir / "state"


# ---------------------------------------------------------------------------
# Test 10: Neither set → marker at repo-relative .vnx-data/state/
# ---------------------------------------------------------------------------

def test_repo_relative_fallback(tmp_path: Path) -> None:
    expected = srt._REPO_ROOT / ".vnx-data" / "state"

    with mock.patch("vnx_paths.resolve_paths", return_value={"VNX_STATE_DIR": str(expected)}):
        result_dir = srt._resolve_state_dir()

    assert result_dir == expected


# ---------------------------------------------------------------------------
# Test 11: CLI entry always exits 0 (fired-successfully AND throttled are both valid)
# ---------------------------------------------------------------------------

def test_cli_returns_0_on_success(tmp_path: Path) -> None:
    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()):
        with mock.patch("sys.exit") as mock_exit:
            srt.maybe_trigger_state_rebuild()
            sys.exit(0)
        mock_exit.assert_called_with(0)


def test_cli_returns_0_on_throttled(tmp_path: Path) -> None:
    throttle = tmp_path / ".last_state_rebuild_ts"
    throttle.write_text(str(int(time.time())), encoding="utf-8")

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path):
        with mock.patch("sys.exit") as mock_exit:
            srt.maybe_trigger_state_rebuild()
            sys.exit(0)
        mock_exit.assert_called_with(0)


# ---------------------------------------------------------------------------
# Test 12: Atomic marker write (intermediate .tmp file used)
# ---------------------------------------------------------------------------

def test_atomic_marker_write_uses_tmp(tmp_path: Path) -> None:
    tmp_files_seen: list[str] = []
    original_write_text = Path.write_text

    def capturing_write_text(self, data, *args, **kwargs):
        tmp_files_seen.append(self.name)
        return original_write_text(self, data, *args, **kwargs)

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()), \
         mock.patch.object(Path, "write_text", capturing_write_text):
        srt.maybe_trigger_state_rebuild()

    assert any(name.endswith(".tmp") for name in tmp_files_seen), (
        f"Expected a .tmp write; saw: {tmp_files_seen}"
    )


# ---------------------------------------------------------------------------
# Test 13: start_new_session=True passed to Popen
# ---------------------------------------------------------------------------

def test_popen_called_with_start_new_session(tmp_path: Path) -> None:
    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()) as mock_p:
        srt.maybe_trigger_state_rebuild()

    call_kwargs = mock_p.call_args
    assert call_kwargs[1].get("start_new_session") is True


# ---------------------------------------------------------------------------
# Test 14: mkdir called if state_dir doesn't exist
# ---------------------------------------------------------------------------

def test_mkdir_if_state_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent" / "state"
    assert not missing.exists()

    with mock.patch.object(srt, "_resolve_state_dir", return_value=missing), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()):
        result = srt.maybe_trigger_state_rebuild()

    assert result is True
    assert missing.exists()


# ---------------------------------------------------------------------------
# Test 15: Two calls 31s apart → both fire (simulated via frozen time)
# ---------------------------------------------------------------------------

def test_two_calls_31s_apart_both_fire(tmp_path: Path) -> None:
    base_time = int(time.time())

    call_count = [0]

    def fake_time():
        # First call at base, second call at base+31
        return base_time if call_count[0] == 0 else base_time + 31

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", return_value=_mock_popen()) as mock_p, \
         mock.patch("state_rebuild_trigger.time.time", fake_time):

        call_count[0] = 0
        r1 = srt.maybe_trigger_state_rebuild(throttle_seconds=30)
        call_count[0] = 1
        r2 = srt.maybe_trigger_state_rebuild(throttle_seconds=30)

    assert r1 is True, "first call should fire"
    assert r2 is True, "second call 31s later should also fire"
    assert mock_p.call_count == 2


# ---------------------------------------------------------------------------
# Test 16: Two concurrent threads — only one fires Popen (lock deduplication)
# ---------------------------------------------------------------------------

def test_concurrent_calls_dedupe(tmp_path: Path) -> None:
    """Two simultaneous calls — only one fires Popen."""
    fired = []
    real_popen = subprocess.Popen

    def slow_popen(*args, **kwargs):
        fired.append(args)
        time.sleep(0.5)  # hold lock long enough for second caller to see contention
        return real_popen(
            ["true"],
            **{k: v for k, v in kwargs.items() if k in ("stdout", "stderr", "start_new_session")},
        )

    results = []

    with mock.patch.object(srt, "_resolve_state_dir", return_value=tmp_path), \
         mock.patch("state_rebuild_trigger.subprocess.Popen", slow_popen):
        threads = [
            threading.Thread(target=lambda: results.append(srt.maybe_trigger_state_rebuild()))
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sum(1 for r in results if r) == 1, f"expected exactly one True, got {results}"
    assert len(fired) == 1, f"expected exactly one Popen call, got {len(fired)}"
