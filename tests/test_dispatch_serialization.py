"""test_dispatch_serialization.py — Tests for serialize_lane + force_release (PR-6).

Covers: intra-thread serialization, no-op for None, exception release,
force_release escape, account-level lock directory resolution, pid liveness
warnings, unexpected OSError re-raise, and timezone-aware _iso_now.
"""
from __future__ import annotations

import errno
import json
import os
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_serialization as _ds_mod
from dispatch_serialization import _iso_now, force_release, serialize_lane


# ---------------------------------------------------------------------------
# test_parallel_claude_serializes
# ---------------------------------------------------------------------------

def test_parallel_claude_serializes(tmp_path, monkeypatch):
    """Two threads entering serialize_lane("claude-tmux") never hold body concurrently."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    concurrent_count = 0
    overlap_detected = False
    count_lock = threading.Lock()

    def worker():
        nonlocal concurrent_count, overlap_detected
        with serialize_lane("claude-tmux", dispatch_id="test-serial"):
            with count_lock:
                concurrent_count += 1
                if concurrent_count > 1:
                    overlap_detected = True
            time.sleep(0.05)
            with count_lock:
                concurrent_count -= 1

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive(), "thread 1 did not finish within timeout"
    assert not t2.is_alive(), "thread 2 did not finish within timeout"
    assert not overlap_detected, "two threads held serialize_lane body concurrently"


# ---------------------------------------------------------------------------
# test_provider_lanes_stay_parallel
# ---------------------------------------------------------------------------

def test_provider_lanes_stay_parallel(tmp_path, monkeypatch):
    """Two concurrent serialize_lane(None) callers both enter body concurrently (no blocking)."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    barrier = threading.Barrier(2, timeout=5)
    both_entered = threading.Event()
    errors = []

    def worker():
        try:
            with serialize_lane(None):
                barrier.wait()  # both must reach here simultaneously
                both_entered.set()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"unexpected errors in provider-lane workers: {errors}"
    assert both_entered.is_set(), "provider lanes did not enter body concurrently"
    # Lock file must NOT be created for None lanes
    assert not (tmp_path / "locks" / "None.lock").exists()


# ---------------------------------------------------------------------------
# test_claude_headless_not_locked
# ---------------------------------------------------------------------------

def test_claude_headless_not_locked(tmp_path, monkeypatch):
    """serialize_lane(None) (headless) is a no-op and does not block a concurrent claude-tmux holder."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    tmux_holding = threading.Event()
    headless_elapsed = []
    errors = []

    def claude_tmux_worker():
        try:
            with serialize_lane("claude-tmux", dispatch_id="tmux-holder"):
                tmux_holding.set()
                time.sleep(0.3)
        except Exception as exc:
            errors.append(("tmux", exc))

    def headless_worker():
        tmux_holding.wait(timeout=5)
        t_start = time.monotonic()
        try:
            with serialize_lane(None):
                pass  # should not block
        except Exception as exc:
            errors.append(("headless", exc))
        headless_elapsed.append(time.monotonic() - t_start)

    t1 = threading.Thread(target=claude_tmux_worker)
    t2 = threading.Thread(target=headless_worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"unexpected errors: {errors}"
    assert headless_elapsed, "headless worker did not complete"
    assert headless_elapsed[0] < 0.15, (
        f"headless lane blocked unexpectedly ({headless_elapsed[0]:.3f}s)"
    )


# ---------------------------------------------------------------------------
# test_lock_released_on_exception
# ---------------------------------------------------------------------------

def test_lock_released_on_exception(tmp_path, monkeypatch):
    """Exception inside serialize_lane body releases the lock; a subsequent acquire succeeds."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    with pytest.raises(RuntimeError, match="intentional test error"):
        with serialize_lane("claude-tmux", dispatch_id="will-fail"):
            raise RuntimeError("intentional test error")

    # After exception, a new acquire must succeed immediately (not deadlock)
    acquired = threading.Event()

    def try_acquire():
        with serialize_lane("claude-tmux", dispatch_id="after-exception"):
            acquired.set()

    t = threading.Thread(target=try_acquire)
    t.start()
    t.join(timeout=5)

    assert acquired.is_set(), "lock was not released after exception in with-body"


# ---------------------------------------------------------------------------
# test_force_release
# ---------------------------------------------------------------------------

def test_force_release(tmp_path, monkeypatch, capsys):
    """force_release prints prior holder and removes lock file; new acquire succeeds after."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "claude-tmux.lock"

    # Write a stale lock file — simulates a prior holder whose process exited
    # without releasing (or whose pid no longer exists).
    stale_meta = {
        "pid": 99999,
        "dispatch_id": "stale-dispatch-id",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    lock_file.write_text(json.dumps(stale_meta))

    force_release("claude-tmux")
    captured = capsys.readouterr()

    assert "stale-dispatch-id" in captured.out, "prior dispatch_id not printed"
    assert "99999" in captured.out, "prior pid not printed"
    assert not lock_file.exists(), "lock file not removed by force_release"

    # Confirm a fresh acquire succeeds post-release
    acquired = threading.Event()

    def try_acquire():
        with serialize_lane("claude-tmux", dispatch_id="post-release"):
            acquired.set()

    t = threading.Thread(target=try_acquire)
    t.start()
    t.join(timeout=5)

    assert acquired.is_set(), "new acquire failed after force_release"


# ---------------------------------------------------------------------------
# test_lock_dir_is_account_level
# ---------------------------------------------------------------------------

def test_lock_dir_is_account_level(tmp_path, monkeypatch):
    """Lock resolves under VNX_LOCK_DIR, not the repo-local VNX_DATA_DIR."""
    account_lock_dir = tmp_path / "account-locks"
    project_data_dir = tmp_path / "project-data"
    project_data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_LOCK_DIR", str(account_lock_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(project_data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with serialize_lane("claude-tmux", dispatch_id="dir-scope-test"):
        # Lock file must be in VNX_LOCK_DIR
        assert (account_lock_dir / "claude-tmux.lock").exists(), (
            "lock file not created in VNX_LOCK_DIR"
        )
        # Lock file must NOT be in VNX_DATA_DIR
        assert not (project_data_dir / "claude-tmux.lock").exists(), (
            "lock file incorrectly created in project VNX_DATA_DIR"
        )

    # Default (no VNX_LOCK_DIR) resolves to ~/.vnx-data/locks — verify shape
    monkeypatch.delenv("VNX_LOCK_DIR", raising=False)
    from dispatch_serialization import _lock_dir
    default_dir = _lock_dir()
    home = Path.home()
    assert default_dir == home / ".vnx-data" / "locks", (
        f"default lock dir {default_dir} is not ~/.vnx-data/locks"
    )


# ---------------------------------------------------------------------------
# test_force_release_warns_on_live_holder
# ---------------------------------------------------------------------------

def test_force_release_warns_on_live_holder(tmp_path, monkeypatch, capsys):
    """force_release prints LOUD double-run warning when holder pid is still alive."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "claude-tmux.lock"

    # Current pid is guaranteed alive
    live_meta = {
        "pid": os.getpid(),
        "dispatch_id": "live-dispatch",
        "timestamp": "2026-06-15T00:00:00Z",
    }
    lock_file.write_text(json.dumps(live_meta))

    force_release("claude-tmux")
    captured = capsys.readouterr()

    assert "STILL ALIVE" in captured.out, "live-holder warning not printed"
    assert "PARALLEL" in captured.out or "double-run" in captured.out.lower(), (
        "double-run risk not mentioned in live-holder warning"
    )
    assert not lock_file.exists(), "lock file not removed after force_release on live holder"


# ---------------------------------------------------------------------------
# test_force_release_dead_holder_no_warning
# ---------------------------------------------------------------------------

def test_force_release_dead_holder_no_warning(tmp_path, monkeypatch, capsys):
    """force_release notes safe removal when holder pid is already dead; no live warning."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "claude-tmux.lock"

    dead_meta = {
        "pid": 999999,  # safely outside any realistic PID range on macOS/Linux
        "dispatch_id": "dead-dispatch",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    lock_file.write_text(json.dumps(dead_meta))

    force_release("claude-tmux")
    captured = capsys.readouterr()

    assert "STILL ALIVE" not in captured.out, (
        "false live-holder warning printed for a dead pid"
    )
    assert not lock_file.exists(), "lock file not removed for dead holder"
    # Should note that the holder is gone
    assert "already gone" in captured.out or "dead-dispatch" in captured.out


# ---------------------------------------------------------------------------
# test_acquire_reraises_unexpected_oserror
# ---------------------------------------------------------------------------

def test_acquire_reraises_unexpected_oserror(tmp_path, monkeypatch):
    """_acquire_with_warn re-raises OSError(EBADF) immediately without spinning."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    call_count = 0

    def bad_flock(fd, op):
        nonlocal call_count
        call_count += 1
        raise OSError(errno.EBADF, "bad file descriptor")

    monkeypatch.setattr(_ds_mod.fcntl, "flock", bad_flock)

    with pytest.raises(OSError, match="bad file descriptor"):
        with serialize_lane("claude-tmux", dispatch_id="badf-test"):
            pass  # must not reach here

    # Must raise on first call — not spin
    assert call_count == 1, f"flock called {call_count} times; expected 1 (no spin on EBADF)"


# ---------------------------------------------------------------------------
# test_iso_now_is_timezone_aware
# ---------------------------------------------------------------------------

def test_iso_now_is_timezone_aware():
    """_iso_now() returns a Z-suffixed UTC timestamp with no DeprecationWarning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        result = _iso_now()

    assert result.endswith("Z"), f"_iso_now() did not end with 'Z': {result!r}"
    # Must parse as timezone-aware
    import datetime
    parsed = datetime.datetime.fromisoformat(result.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, "_iso_now() result is not timezone-aware"
