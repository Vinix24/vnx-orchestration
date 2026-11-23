import multiprocessing
import time

import pytest

import wheel_build_lock as wbl


def _hold(lock_path, started, release_after, log_path):
    with wbl.wheel_build_lock(lock_path, timeout=30):
        with open(log_path, "a") as fh:
            fh.write(f"in {time.monotonic()}\n")
        started.set()
        time.sleep(release_after)
        with open(log_path, "a") as fh:
            fh.write(f"out {time.monotonic()}\n")


def test_second_holder_waits_for_first(tmp_path):
    lock = tmp_path / "sub" / ".wheel-build.lock"
    log = tmp_path / "log.txt"
    ctx = multiprocessing.get_context("fork")
    started = ctx.Event()
    first = ctx.Process(target=_hold, args=(lock, started, 1.0, log))
    first.start()
    assert started.wait(15)
    t0 = time.monotonic()
    with wbl.wheel_build_lock(lock, timeout=30):
        waited = time.monotonic() - t0
        lines = log.read_text().splitlines()
    first.join(15)
    assert [x.split()[0] for x in lines] == ["in", "out"]
    assert waited >= 0.5


def test_timeout_fails_with_clear_message(tmp_path):
    lock = tmp_path / ".wheel-build.lock"
    with wbl.wheel_build_lock(lock):
        with pytest.raises(pytest.fail.Exception, match="not acquired within"):
            with wbl.wheel_build_lock(lock, timeout=0.3):
                pytest.fail("must not enter")  # pragma: no cover


def test_lock_released_after_exit_and_on_error(tmp_path):
    lock = tmp_path / ".wheel-build.lock"
    with pytest.raises(RuntimeError):
        with wbl.wheel_build_lock(lock):
            raise RuntimeError("boom")
    with wbl.wheel_build_lock(lock, timeout=1):
        pass


def test_default_path_is_repo_root_build_folder():
    assert wbl.DEFAULT_LOCK_PATH == wbl.REPO_ROOT / "build" / ".wheel-build.lock"
    assert wbl.DEFAULT_TIMEOUT_S == 600.0
