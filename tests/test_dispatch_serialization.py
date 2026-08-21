"""test_dispatch_serialization.py — Tests for serialize_lane + force_release (PR-6).

Covers: intra-thread serialization, no-op for None, exception release,
force_release escape, account-level lock directory resolution, pid liveness
warnings, unexpected OSError re-raise, timezone-aware _iso_now, and the
registry-backed VNX_TMUX_MAX_CONCURRENT precedence (env > config-store > default).
"""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import config_registry as _cr_mod
import config_runtime as _crt_mod
import config_store_db as _csdb_mod
import dispatch_serialization as _ds_mod
from dispatch_serialization import _iso_now, force_release, serialize_lane


# ---------------------------------------------------------------------------
# test_parallel_claude_serializes
# ---------------------------------------------------------------------------

def test_parallel_claude_serializes(tmp_path, monkeypatch):
    """Two threads entering serialize_lane("claude-tmux") never hold body concurrently.

    Pins VNX_TMUX_MAX_CONCURRENT=1 explicitly (the unset default is now 10 — see
    test_max_concurrent_defaults_to_ten) — this test is about single-slot mutual
    exclusion, not the default concurrency level; test_three_slots_concurrent_fourth_blocks
    below covers N>1."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("VNX_TMUX_MAX_CONCURRENT", "1")

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
# test_three_slots_concurrent_fourth_blocks
# ---------------------------------------------------------------------------

def test_three_slots_concurrent_fourth_blocks(tmp_path, monkeypatch):
    """VNX_TMUX_MAX_CONCURRENT=3: three concurrent holders run simultaneously;
    a fourth waiter blocks until one of the three releases."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("VNX_TMUX_MAX_CONCURRENT", "3")

    concurrent_count = 0
    max_concurrent_seen = 0
    count_lock = threading.Lock()
    release_gate = threading.Event()  # holds the first 3 workers inside the body
    fourth_entered = threading.Event()
    errors = []

    def holder_worker(idx):
        nonlocal concurrent_count, max_concurrent_seen
        try:
            with serialize_lane("claude-tmux", dispatch_id=f"holder-{idx}"):
                with count_lock:
                    concurrent_count += 1
                    max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
                release_gate.wait(timeout=5)
                with count_lock:
                    concurrent_count -= 1
        except Exception as exc:
            errors.append((f"holder-{idx}", exc))

    def fourth_worker():
        try:
            with serialize_lane("claude-tmux", dispatch_id="fourth"):
                fourth_entered.set()
        except Exception as exc:
            errors.append(("fourth", exc))

    holders = [threading.Thread(target=holder_worker, args=(i,)) for i in range(3)]
    for t in holders:
        t.start()

    # Wait until all three holders are confirmed inside the body.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and concurrent_count < 3:
        time.sleep(0.02)
    assert concurrent_count == 3, "three holders did not all acquire concurrently"

    fourth = threading.Thread(target=fourth_worker)
    fourth.start()
    # Fourth must NOT enter while all 3 slots are held.
    assert not fourth_entered.wait(timeout=0.3), "fourth acquired before a slot freed"

    release_gate.set()  # let the three holders release
    for t in holders:
        t.join(timeout=5)
    fourth.join(timeout=5)

    assert not errors, f"unexpected errors: {errors}"
    assert max_concurrent_seen == 3, f"expected 3 concurrent holders, saw {max_concurrent_seen}"
    assert fourth_entered.is_set(), "fourth waiter never acquired after a slot freed"


# ---------------------------------------------------------------------------
# test_max_concurrent clamping + defaults
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_config_registry_globals():
    """config_registry's db-resolver / default-project and config_runtime's autowire
    cache are module-level globals shared across the whole pytest session — reset
    them around every test in this file so a config-store test below can never leak
    a wired DB resolver into an unrelated env-only test (or vice versa)."""
    _cr_mod.set_db_resolver(None)
    _cr_mod.set_default_project_id(None)
    _crt_mod._wired_for.clear()
    yield
    _cr_mod.set_db_resolver(None)
    _cr_mod.set_default_project_id(None)
    _crt_mod._wired_for.clear()


def test_max_concurrent_defaults_to_ten(monkeypatch):
    """dispatch-20260821-t0-tmux-concurrency-10: no VNX_TMUX_MAX_CONCURRENT env var, no
    config-store row -> default concurrency is 10 (raised from 5, operator directive
    2026-08-21; 5 itself was raised from 1 now that #1451 gives every tmux dispatch its
    own named paste buffer — see the module docstring)."""
    monkeypatch.delenv("VNX_TMUX_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    assert _ds_mod._max_concurrent() == 10


def test_max_concurrent_accepts_valid_positive_value(monkeypatch):
    """A valid positive integer is used verbatim, overriding the default."""
    monkeypatch.setenv("VNX_TMUX_MAX_CONCURRENT", "3")
    assert _ds_mod._max_concurrent() == 3


@pytest.mark.parametrize("raw_value", ["0", "-1", "-100", "x", ""])
def test_max_concurrent_clamps_bad_values(raw_value, monkeypatch):
    """0, negative, or unparseable VNX_TMUX_MAX_CONCURRENT falls back to 10 (>= 1 satisfied,
    same fallback as the missing-env-var default — see test_max_concurrent_defaults_to_ten)."""
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setenv("VNX_TMUX_MAX_CONCURRENT", raw_value)
    result = _ds_mod._max_concurrent()
    assert result >= 1
    assert result == 10


def test_max_concurrent_reads_config_store_value(tmp_path, monkeypatch):
    """VNX_TMUX_MAX_CONCURRENT registry row (OI-1412): a config-store value with NO env
    var set must be genuinely read via config_runtime -- a registry row nothing reads
    is decoration, not config. Proven by writing through config_store_db (the same
    write path the dashboard uses), not by stubbing the resolver."""
    monkeypatch.delenv("VNX_TMUX_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_TMUX_MAX_CONCURRENT", raising=False)

    sd = tmp_path / "state"
    sd.mkdir()
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    _csdb_mod.set_config(
        conn, "vnx-dev", "VNX_TMUX_MAX_CONCURRENT", "7",
        actor="op", approval_id="test-approval",
    )
    conn.close()

    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

    assert _ds_mod._max_concurrent() == 7


def test_max_concurrent_env_wins_over_config_store(tmp_path, monkeypatch):
    """The process env var is an explicit per-session override and must win over a
    persisted config-store row for the same key, even when the store is wired and
    has a value -- precedence is env > config-store > default."""
    sd = tmp_path / "state"
    sd.mkdir()
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    _csdb_mod.set_config(
        conn, "vnx-dev", "VNX_TMUX_MAX_CONCURRENT", "7",
        actor="op", approval_id="test-approval",
    )
    conn.close()

    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.setenv("VNX_TMUX_MAX_CONCURRENT", "4")

    assert _ds_mod._max_concurrent() == 4


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
# test_claude_tmux_and_headless_share_lock (OI-1417)
# ---------------------------------------------------------------------------

def test_claude_tmux_and_headless_share_lock(tmp_path, monkeypatch):
    """OI-1417: dispatch_plan.py's D5 resolves BOTH claude lanes (tmux and
    headless) to the same serialization_class="claude-tmux" string, because
    both authenticate against the same subscription. This is the door-level
    call the CLI actually makes for a headless dispatch — serialize_lane(None)
    is no longer what a headless plan produces. With N=1, a tmux holder must
    block a concurrent headless caller until it releases; they must not run
    past each other."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("VNX_TMUX_MAX_CONCURRENT", "1")

    tmux_holding = threading.Event()
    release_gate = threading.Event()
    headless_entered = threading.Event()
    errors = []

    def claude_tmux_worker():
        try:
            with serialize_lane("claude-tmux", dispatch_id="tmux-holder"):
                tmux_holding.set()
                release_gate.wait(timeout=5)
        except Exception as exc:
            errors.append(("tmux", exc))

    def headless_worker():
        tmux_holding.wait(timeout=5)
        try:
            with serialize_lane("claude-tmux", dispatch_id="headless-holder"):
                headless_entered.set()
        except Exception as exc:
            errors.append(("headless", exc))

    t1 = threading.Thread(target=claude_tmux_worker)
    t2 = threading.Thread(target=headless_worker)
    t1.start()
    t2.start()

    tmux_holding.wait(timeout=5)
    assert not headless_entered.wait(timeout=0.3), (
        "headless caller acquired the lock while the tmux holder still held it "
        "— they crossed each other instead of colliding on the shared N"
    )

    release_gate.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"unexpected errors: {errors}"
    assert headless_entered.is_set(), "headless caller never acquired after tmux released"


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
    lock_file = lock_dir / "claude-tmux-slot-0.lock"

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
        assert (account_lock_dir / "claude-tmux-slot-0.lock").exists(), (
            "lock file not created in VNX_LOCK_DIR"
        )
        # Lock file must NOT be in VNX_DATA_DIR
        assert not (project_data_dir / "claude-tmux-slot-0.lock").exists(), (
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
    lock_file = lock_dir / "claude-tmux-slot-0.lock"

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
    lock_file = lock_dir / "claude-tmux-slot-0.lock"

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
# test_holder_metadata_cleared_on_release (OI-844)
# ---------------------------------------------------------------------------

def test_holder_metadata_cleared_on_normal_release(tmp_path, monkeypatch):
    """After a normal (no-exception) release, the slot lock file no longer
    carries the released holder's pid/dispatch_id — it reads as unheld to
    force_release() and any diagnostic reader, not as still-OCCUPIED."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    with serialize_lane("claude-tmux", dispatch_id="metadata-clear-test"):
        pass  # acquire then release normally

    lock_file = tmp_path / "locks" / "claude-tmux-slot-0.lock"
    assert lock_file.exists(), "lock file should still exist after release (only flock releases)"
    content = lock_file.read_bytes()
    assert content == b"", f"holder metadata not cleared on release: {content!r}"


def test_holder_metadata_cleared_on_exception_release(tmp_path, monkeypatch):
    """The error release path also clears holder metadata — not just the happy path."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    with pytest.raises(RuntimeError, match="intentional test error"):
        with serialize_lane("claude-tmux", dispatch_id="metadata-clear-exc-test"):
            raise RuntimeError("intentional test error")

    lock_file = tmp_path / "locks" / "claude-tmux-slot-0.lock"
    content = lock_file.read_bytes()
    assert content == b"", f"holder metadata not cleared on exception release: {content!r}"


def test_force_release_after_clean_release_shows_no_prior_holder(tmp_path, monkeypatch, capsys):
    """A released (metadata-cleared) slot must not still report the prior
    holder's dispatch_id/pid to force_release — the measured bug this fix closes."""
    monkeypatch.setenv("VNX_LOCK_DIR", str(tmp_path / "locks"))

    with serialize_lane("claude-tmux", dispatch_id="should-not-leak"):
        pass

    force_release("claude-tmux")
    captured = capsys.readouterr()
    assert "should-not-leak" not in captured.out, (
        "force_release still reported a dispatch_id from a released slot"
    )


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
