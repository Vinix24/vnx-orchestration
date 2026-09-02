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

# Per-write completion budget in seconds, used ONLY as a reference value in
# the OI-1598 proof below (and to size the OI-1598 slow-runner delay) — it
# is no longer used to compute a pass/fail deadline. The measured unloaded
# cost of one append_locked call is ~0.08ms, so 12ms is ~150x headroom.
# That per-write constant is exactly the defect OI-1598 fixes: a budget
# scaled by workload size (_THREADS * _WRITES_PER_THREAD * this constant)
# is still a FIXED per-write time assumption, so it still false-fails when
# the runner itself (not just the workload) is slower than assumed — CI
# measured one run ~80% slower than another and it blew even the scaled
# budget while making progress the whole time. The wait logic below no
# longer measures total time at all; it measures whether writes are still
# landing. The old fixed 10s join (pre-dating the scaled budget) was sized
# for an unloaded machine and false-failed the same way, one layer earlier
# (OI-1005).
_PER_WRITE_BUDGET_S = 0.012

# How long the wait tolerates ZERO completions across ALL threads before
# calling it a stall. Deliberately NOT scaled to workload size — that
# scaling is the bug class this fixes. It only has to be orders of
# magnitude above any single flock+fsync cycle we simulate (unloaded
# ~0.08ms; the injected slow-runner delays below top out in the tens of
# ms) so real progress on a slow-but-healthy runner never trips it, while
# a genuine deadlock still fails in seconds rather than tying up CI.
_STALL_WINDOW_S = 3.0


class _ProgressCounter:
    """Thread-safe monotonic counter of completed writes.

    A lock-guarded int is the cheapest counter that's actually reliable
    across threads: the lock costs nanoseconds against the ~80us-1.5ms
    flock+fsync cycle it counts, so it can't skew the thing it measures.
    """

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:
            self._value += 1

    @property
    def value(self) -> int:
        return self._value


def _wait_for_progress(
    threads: list[threading.Thread],
    *,
    progress: _ProgressCounter,
    total: int,
    stall_window: float,
) -> list[threading.Thread]:
    """Wait for every thread to finish, judging health by progress, not time.

    Polls the shared completion counter instead of comparing elapsed
    wall-clock time to a deadline: as long as SOME write lands within each
    `stall_window`, the wait keeps going no matter how long the whole run
    takes, so a slower machine just takes longer without failing. Only a
    genuine stall — nothing completes for a full `stall_window` — ends the
    wait early. Returns the threads still alive when either all `total`
    writes have landed and every thread has exited, or the stall window
    elapses without a new completion.
    """
    poll_interval = min(0.05, stall_window / 10)
    last_seen = progress.value
    last_progress_at = time.monotonic()
    while any(thread.is_alive() for thread in threads):
        if progress.value >= total:
            # All writes landed; give any thread still unwinding from its
            # last call a real chance to exit before checking is_alive().
            for thread in threads:
                thread.join(timeout=stall_window)
            break
        time.sleep(poll_interval)
        seen = progress.value
        if seen > last_seen:
            last_seen = seen
            last_progress_at = time.monotonic()
        elif time.monotonic() - last_progress_at > stall_window:
            break
    return [thread for thread in threads if thread.is_alive()]


def _slow_append_factory(delay_s: float):
    """Wrap the real append_locked with a delay held inside its lock.

    The delay must sit inside the same sentinel lock append_locked uses so
    it actually serializes (a delay before the lock would run in parallel
    across threads and change nothing) — this is how a slow-runner's
    per-operation cost is faithfully simulated.
    """
    real_append = SW.append_locked

    def _slow_append(data_path: Path, record: dict) -> None:
        sentinel = SW._sentinel_path(data_path)
        with sentinel.open("a+", encoding="utf-8") as sentinel_fh:
            fcntl.flock(sentinel_fh.fileno(), fcntl.LOCK_EX)
            time.sleep(delay_s)
        real_append(data_path, record)

    return _slow_append


def test_append_locked_writes_single_record(tmp_path: Path):
    path = tmp_path / "state.ndjson"

    SW.append_locked(path, {"event": "dispatch_created", "dispatch_id": "d-001"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "event": "dispatch_created",
        "dispatch_id": "d-001",
    }


def test_append_locked_skip_if_sees_content_inside_lock_and_skips(tmp_path: Path):
    """OI-1129: skip_if receives the file's current bytes INSIDE the critical
    section; True skips the append and append_locked reports False."""
    path = tmp_path / "state.ndjson"
    SW.append_locked(path, {"dispatch_id": "d-1"})
    seen: list[bytes] = []

    def _skip(content: bytes) -> bool:
        seen.append(content)
        return b"d-1" in content

    result = SW.append_locked(path, {"dispatch_id": "d-1"}, skip_if=_skip)

    assert result is False
    assert seen and b'"dispatch_id":"d-1"' in seen[0]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_append_locked_skip_if_false_appends_and_returns_true(tmp_path: Path):
    path = tmp_path / "state.ndjson"

    result = SW.append_locked(path, {"dispatch_id": "d-2"}, skip_if=lambda content: False)

    assert result is True
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"dispatch_id": "d-2"}


def test_append_locked_without_skip_if_returns_true(tmp_path: Path):
    path = tmp_path / "state.ndjson"

    assert SW.append_locked(path, {"dispatch_id": "d-3"}) is True


def test_append_locked_concurrent_100_threads_100_writes(tmp_path: Path):
    path = tmp_path / "state.ndjson"
    start = threading.Event()
    progress = _ProgressCounter()

    def _worker(index: int) -> None:
        start.wait(timeout=5)
        for seq in range(_WRITES_PER_THREAD):
            SW.append_locked(path, {"thread": index, "seq": seq})
            progress.increment()

    threads = [
        threading.Thread(target=_worker, args=(index,), daemon=True)
        for index in range(_THREADS)
    ]

    for thread in threads:
        thread.start()

    start.set()

    # The writes are serialized by append_locked's flock, so the workload is
    # _THREADS * _WRITES_PER_THREAD sequential flock+fsync cycles. Waiting on
    # progress rather than a wall-clock deadline means a slow-but-advancing
    # runner just takes longer instead of false-failing (OI-1005, OI-1598);
    # a genuine lock regression (a thread blocked forever) still trips the
    # stall window below.
    total = _THREADS * _WRITES_PER_THREAD
    alive = _wait_for_progress(
        threads, progress=progress, total=total, stall_window=_STALL_WINDOW_S
    )
    assert not alive, (
        f"{len(alive)} worker thread(s) made no progress for "
        f"{_STALL_WINDOW_S:.0f}s ({progress.value}/{total} writes landed). "
        "append_locked likely deadlocked"
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
    before any contention overhead. The progress-based wait must let the
    workload finish regardless of total elapsed time; if the wait logic
    reverts to a fixed <=10s join, this test turns red.
    """
    path = tmp_path / "state.ndjson"
    start = threading.Event()
    progress = _ProgressCounter()
    simulated_delay_s = 0.0015

    monkeypatch.setattr(SW, "append_locked", _slow_append_factory(simulated_delay_s))

    def _worker(index: int) -> None:
        start.wait(timeout=5)
        for seq in range(_WRITES_PER_THREAD):
            SW.append_locked(path, {"thread": index, "seq": seq})
            progress.increment()

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

    total = _THREADS * _WRITES_PER_THREAD
    alive = _wait_for_progress(
        threads, progress=progress, total=total, stall_window=_STALL_WINDOW_S
    )
    assert not alive, (
        f"{len(alive)} worker thread(s) made no progress for "
        f"{_STALL_WINDOW_S:.0f}s under simulated load "
        f"({progress.value}/{total} writes landed)"
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == _THREADS * _WRITES_PER_THREAD
    for line in lines:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)


def test_oi1598_progress_wait_tolerates_a_slow_runner_that_breaks_scaled_budget(
    tmp_path: Path, monkeypatch
):
    """Regression for OI-1598.

    OI-1005's fix scaled the completion budget to the workload size
    (_THREADS * _WRITES_PER_THREAD * _PER_WRITE_BUDGET_S), but that budget
    is still a FIXED per-write time assumption — it moved the problem from
    "vast getal" to "vast getal per eenheid werk" without closing it. CI
    measured one suite run ~80% slower than a prior one (2244.91s vs
    1252.84s wall-clock) and it blew even the scaled budget while making
    progress the entire time (98 threads still had unfinished writes, not
    zero).

    This is the direct proof, not just a claim: inject a per-write delay
    ABOVE _PER_WRITE_BUDGET_S so the OLD scaled-budget assertion is
    provably false for THIS run's own measured elapsed time, then show the
    progress-based wait still succeeds because writes keep landing. Small
    workload on purpose (5x5, not 100x100) — the inequality that matters
    (per-write delay > per-write budget) holds independent of workload
    size, so this stays fast while proving the same failure mode.
    """
    path = tmp_path / "state.ndjson"
    start = threading.Event()
    progress = _ProgressCounter()
    threads_n, writes_n = 5, 5
    total = threads_n * writes_n
    # 3x _PER_WRITE_BUDGET_S: comfortably above the old per-write allowance
    # (so the old scaled-budget assertion is guaranteed false below) while
    # comfortably below _STALL_WINDOW_S (so the new wait still sees steady
    # progress, not a stall).
    slow_delay_s = _PER_WRITE_BUDGET_S * 3

    monkeypatch.setattr(SW, "append_locked", _slow_append_factory(slow_delay_s))

    def _worker(index: int) -> None:
        start.wait(timeout=5)
        for seq in range(writes_n):
            SW.append_locked(path, {"thread": index, "seq": seq})
            progress.increment()

    threads = [
        threading.Thread(target=_worker, args=(index,), daemon=True)
        for index in range(threads_n)
    ]

    for thread in threads:
        thread.start()

    run_start = time.monotonic()
    start.set()

    alive = _wait_for_progress(
        threads, progress=progress, total=total, stall_window=_STALL_WINDOW_S
    )
    elapsed = time.monotonic() - run_start

    old_scaled_budget = total * _PER_WRITE_BUDGET_S
    print(
        f"OI-1598 proof: elapsed={elapsed:.3f}s, old scaled budget="
        f"{old_scaled_budget:.3f}s -> old assertion (elapsed <= budget) "
        f"would be {elapsed <= old_scaled_budget}; new progress assertion "
        f"(not alive) is {not alive}"
    )

    # The OLD assertion, applied to this exact run, must be provably red.
    assert elapsed > old_scaled_budget, (
        f"elapsed {elapsed:.3f}s must exceed the old scaled budget "
        f"{old_scaled_budget:.3f}s for this to prove OI-1598's failure "
        "mode: increase slow_delay_s relative to _PER_WRITE_BUDGET_S"
    )
    # The NEW assertion, on the same run, must be green.
    assert not alive, (
        f"{len(alive)} thread(s) made no progress for {_STALL_WINDOW_S:.0f}s "
        f"even though the injected per-write delay ({slow_delay_s * 1000:.0f}ms) "
        "alone should not stall progress"
    )
    assert progress.value == total

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == total


def test_oi1598_wait_for_progress_detects_a_genuine_hang():
    """The progress-based wait must still catch a real lock regression.

    A thread that blocks forever makes zero progress, so it's returned
    when the stall window expires — a red run keeps meaning "append_locked
    deadlocked" rather than "the runner was slow" (OI-1005, OI-1598).
    """
    release = threading.Event()
    entered = threading.Event()
    progress = _ProgressCounter()

    def _blocked() -> None:
        entered.set()
        release.wait(timeout=30)

    thread = threading.Thread(target=_blocked, daemon=True)
    thread.start()
    entered.wait(timeout=5)

    alive = _wait_for_progress(
        [thread], progress=progress, total=1, stall_window=0.5
    )
    release.set()
    thread.join(timeout=5)

    assert alive == [thread]


def test_sentinel_registry_historical_names(tmp_path: Path):
    path = tmp_path / "dispatch_register.ndjson"

    assert SW._sentinel_path(path) == tmp_path / ".state.lock"


def test_sentinel_registry_default_pattern(tmp_path: Path):
    path = tmp_path / "foo.ndjson"

    assert SW._sentinel_path(path) == tmp_path / ".foo.ndjson.sentinel.lock"
