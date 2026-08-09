from __future__ import annotations

import fcntl
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import state_writer as SW  # noqa: E402

# Workload of the concurrent test below: 100 threads each appending 100
# records. append_locked serializes every write behind flock, so the real
# work is THREADS * WRITES_PER_THREAD sequential flock+fsync cycles.
_THREADS = 100
_WRITES_PER_THREAD = 100

# Per-write completion budget in seconds. The measured unloaded cost of one
# append_locked call is ~0.08ms (the full workload runs in ~0.8s), so the
# 12ms allowance is ~150x headroom: generous enough for a loaded shared
# runner to finish, tight enough that a genuine lock regression (a thread
# blocked forever) still trips the assert. The old fixed 10s join was sized
# for an unloaded machine and false-failed on the CI runner (OI-1005).
_PER_WRITE_BUDGET_S = 0.012


def _wait_for_threads(
    threads: list[threading.Thread], *, completion_budget: float
) -> list[threading.Thread]:
    """Join every thread within one shared completion budget.

    Each join gets whatever remains of the budget, so the deadline is a
    wall-clock hang-detector rather than a per-thread allowance: a
    slow-but-progressing runner finishes (all threads do identical work and
    start together), a deadlocked thread blocks forever and trips the
    deadline. Returns the threads still alive when the budget expires.
    """
    deadline = time.monotonic() + completion_budget
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return [thread for thread in threads if thread.is_alive()]


def test_append_locked_writes_single_record(tmp_path: Path):
    path = tmp_path / "state.ndjson"

    SW.append_locked(path, {"event": "dispatch_created", "dispatch_id": "d-001"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "event": "dispatch_created",
        "dispatch_id": "d-001",
    }


def test_append_locked_concurrent_100_threads_100_writes(tmp_path: Path):
    path = tmp_path / "state.ndjson"
    start = threading.Event()

    def _worker(index: int) -> None:
        start.wait(timeout=5)
        for seq in range(_WRITES_PER_THREAD):
            SW.append_locked(path, {"thread": index, "seq": seq})

    threads = [
        threading.Thread(target=_worker, args=(index,), daemon=True)
        for index in range(_THREADS)
    ]

    for thread in threads:
        thread.start()

    start.set()

    # The writes are serialized by append_locked's flock, so the workload is
    # _THREADS * _WRITES_PER_THREAD sequential flock+fsync cycles. A fixed
    # 10s join was tuned to an unloaded machine and false-failed on a loaded
    # shared runner (OI-1005). The per-write-scaled budget below waits long
    # enough for a slow-but-progressing runner while a genuine lock
    # regression (a thread blocked forever) still trips the assert.
    completion_budget = _THREADS * _WRITES_PER_THREAD * _PER_WRITE_BUDGET_S
    alive = _wait_for_threads(threads, completion_budget=completion_budget)
    assert not alive, (
        f"{len(alive)} worker thread(s) did not finish within "
        f"{completion_budget:.0f}s ({_THREADS} threads x {_WRITES_PER_THREAD} "
        f"writes, {_PER_WRITE_BUDGET_S * 1000:.0f}ms allowance per write). "
        "Either the runner is pathologically slow or append_locked deadlocked"
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == _THREADS * _WRITES_PER_THREAD
    for line in lines:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)


def test_oi1005_slow_runner_fits_scaled_budget_but_not_fixed_join(
    tmp_path: Path, monkeypatch
):
    """Regression for OI-1005.

    A loaded shared runner makes the 10,000 serialized flock+fsync cycles
    slower than the old fixed 10s join, so the concurrent test false-failed
    on a run that was merely slow. Simulate that runner by inflating
    append_locked's per-write cost to ~1.5ms (measured unloaded: ~0.08ms):
    the pure-sleep component alone is ~15s serialized, past the old 10s cap,
    before any contention overhead. The scaled budget must let the workload
    finish; if the wait logic reverts to a fixed <=10s join, this test turns
    red.
    """
    path = tmp_path / "state.ndjson"
    start = threading.Event()
    simulated_delay_s = 0.0015
    real_append = SW.append_locked

    def _slow_append(data_path: Path, record: dict) -> None:
        # Faithful slow-runner simulation. append_locked's work is
        # serialized by its flock critical section, so the injected delay
        # must sit inside that section to actually slow the workload down
        # (a delay before the lock would run in parallel across the 100
        # threads and change nothing). Hold the same sentinel lock
        # append_locked uses, sleep, then release and delegate.
        sentinel = SW._sentinel_path(data_path)
        with sentinel.open("a+", encoding="utf-8") as sentinel_fh:
            fcntl.flock(sentinel_fh.fileno(), fcntl.LOCK_EX)
            time.sleep(simulated_delay_s)
        real_append(data_path, record)

    monkeypatch.setattr(SW, "append_locked", _slow_append)

    def _worker(index: int) -> None:
        start.wait(timeout=5)
        for seq in range(_WRITES_PER_THREAD):
            SW.append_locked(path, {"thread": index, "seq": seq})

    threads = [
        threading.Thread(target=_worker, args=(index,), daemon=True)
        for index in range(_THREADS)
    ]

    for thread in threads:
        thread.start()

    start.set()

    # The simulated workload must actually exceed the old fixed 10s join,
    # or this test proves nothing about the false positive it reproduces.
    serialized_workload = _THREADS * _WRITES_PER_THREAD * simulated_delay_s
    assert serialized_workload > 10.0, (
        f"simulated workload {serialized_workload:.1f}s must exceed the old "
        "fixed 10s join to be a valid slow-runner reproduction"
    )

    completion_budget = _THREADS * _WRITES_PER_THREAD * _PER_WRITE_BUDGET_S
    alive = _wait_for_threads(threads, completion_budget=completion_budget)
    assert not alive, (
        f"{len(alive)} worker thread(s) did not finish within "
        f"{completion_budget:.0f}s under simulated load"
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == _THREADS * _WRITES_PER_THREAD
    for line in lines:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)


def test_oi1005_wait_for_threads_detects_a_genuine_hang():
    """The completion wait must still catch a real lock regression.

    A thread that blocks forever is returned when the budget expires, so a
    red run of the concurrent test keeps meaning "append_locked deadlocked"
    rather than "the runner was slow" (OI-1005).
    """
    release = threading.Event()
    entered = threading.Event()

    def _blocked() -> None:
        entered.set()
        release.wait(timeout=30)

    thread = threading.Thread(target=_blocked, daemon=True)
    thread.start()
    entered.wait(timeout=5)

    alive = _wait_for_threads([thread], completion_budget=0.5)
    release.set()
    thread.join(timeout=5)

    assert alive == [thread]


def test_sentinel_registry_historical_names(tmp_path: Path):
    path = tmp_path / "dispatch_register.ndjson"

    assert SW._sentinel_path(path) == tmp_path / ".state.lock"


def test_sentinel_registry_default_pattern(tmp_path: Path):
    path = tmp_path / "foo.ndjson"

    assert SW._sentinel_path(path) == tmp_path / ".foo.ndjson.sentinel.lock"
