"""R10 (dispatch dlv-38447134aeec): the run lock and the retry-cap count in ``gate_run_guard``.

The lock is a real ``fcntl.flock`` on a real file under a tmp state dir; the
other holder is a real second process (or a second thread of this one). No
gate and no provider is involved.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gate_run_guard as guard  # noqa: E402

PR = 4242
HEAD = "c3" * 20

_HOLDER = """
import fcntl, json, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.pwrite(fd, json.dumps({"pid": os.getpid()}).encode("utf-8"), 0)
print("held", flush=True)
sys.stdin.read()
"""


def _state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    return state_dir


def _hold_elsewhere(path: Path) -> subprocess.Popen:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def _acquire(state_dir: Path, gate: str = "codex_gate", head: str = HEAD) -> guard.RunLock:
    return guard.acquire(state_dir, pr_ref=PR, gate=gate, head_sha=head)


class TestTheLock:
    def test_another_process_holding_the_key_refuses_the_launch_and_is_named(self, tmp_path):
        state_dir = _state(tmp_path)
        path = guard.run_lock_path(state_dir, pr_ref=PR, gate="codex_gate", head_sha=HEAD)
        proc = _hold_elsewhere(path)
        try:
            lock = _acquire(state_dir)
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

        assert lock.acquired is False
        assert lock.holder_pid == proc.pid
        assert lock.reason == guard.IN_FLIGHT_REASON
        assert lock.key in lock.refusal_text() and str(proc.pid) in lock.refusal_text()

    def test_a_dead_holder_blocks_nothing(self, tmp_path):
        state_dir = _state(tmp_path)
        path = guard.run_lock_path(state_dir, pr_ref=PR, gate="codex_gate", head_sha=HEAD)
        proc = _hold_elsewhere(path)
        proc.kill()
        proc.wait(timeout=10)
        assert path.exists(), "a crash leaves the file behind"

        lock = _acquire(state_dir)
        try:
            assert lock.acquired is True, "the kernel released the dead holder's lock"
            assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
        finally:
            lock.release()

    def test_another_head_gate_or_pr_is_another_key(self, tmp_path):
        state_dir = _state(tmp_path)
        first = _acquire(state_dir)
        try:
            for other in (_acquire(state_dir, head="d4" * 20), _acquire(state_dir, gate="kimi_gate"),
                          guard.acquire(state_dir, pr_ref=PR + 1, gate="codex_gate", head_sha=HEAD)):
                assert other.acquired is True
                other.release()
        finally:
            first.release()

    def test_the_same_thread_may_take_a_key_it_holds_and_the_last_release_frees_it(self, tmp_path):
        state_dir = _state(tmp_path)
        outer = _acquire(state_dir)
        inner = _acquire(state_dir)
        assert outer.acquired and inner.acquired

        inner.release()
        assert outer.path.exists(), "the outer holder still holds it"
        proc_path = outer.path
        probe = subprocess.run(
            [sys.executable, "-c",
             "import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\n"
             "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    print('free')\n"
             "except BlockingIOError:\n    print('held')\n", str(proc_path)],
            capture_output=True, text=True, check=True,
        )
        assert probe.stdout.strip() == "held"

        outer.release()
        assert not proc_path.exists(), "the last release unlinks the lock file"

    def test_another_thread_of_this_process_is_refused(self, tmp_path):
        state_dir = _state(tmp_path)
        held = _acquire(state_dir)
        seen: Dict[str, Any] = {}

        def other_thread():
            lock = _acquire(state_dir)
            seen["acquired"], seen["pid"] = lock.acquired, lock.holder_pid
            lock.release()

        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(timeout=10)
        held.release()

        assert seen == {"acquired": False, "pid": os.getpid()}

    def test_a_file_replaced_between_open_and_lock_is_taken_on_the_new_file(self, tmp_path, monkeypatch):
        """The unlink-on-release race: the taker opened the old file, the holder unlinked it."""
        state_dir = _state(tmp_path)
        path = guard.run_lock_path(state_dir, pr_ref=PR, gate="codex_gate", head_sha=HEAD)
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        real_flock = guard.fcntl.flock
        calls = {"n": 0}

        def flock_after_a_release_elsewhere(fd, op):
            calls["n"] += 1
            if calls["n"] == 1:
                path.unlink()
                path.write_text("{}", encoding="utf-8")
            return real_flock(fd, op)

        monkeypatch.setattr(guard.fcntl, "flock", flock_after_a_release_elsewhere)
        lock = _acquire(state_dir)
        try:
            assert lock.acquired is True
            assert calls["n"] == 2, "the stale file was given up and the new one locked"
            fd = guard._HELD[(str(path), threading.get_ident())][0]
            assert os.fstat(fd).st_ino == os.stat(path).st_ino
        finally:
            monkeypatch.setattr(guard.fcntl, "flock", real_flock)
            lock.release()

    def test_a_lock_dir_that_cannot_be_made_refuses_with_an_error(self, tmp_path):
        state_dir = _state(tmp_path)
        (state_dir / "review_gates").mkdir()
        (state_dir / "review_gates" / "run_locks").write_text("not a directory", encoding="utf-8")

        lock = _acquire(state_dir)

        assert lock.acquired is False
        assert lock.reason == guard.LOCK_ERROR_REASON and lock.error

    def test_the_key_names_the_project_of_the_store(self, tmp_path):
        first = tmp_path / "one" / ".vnx-data" / "state"
        second = tmp_path / "two" / ".vnx-data" / "state"
        for state_dir, project in ((first, "proj-one"), (second, "proj-two")):
            state_dir.mkdir(parents=True)
            (state_dir.parent.parent / ".vnx-project-id").write_text(project + "\n", encoding="utf-8")

        name = guard.run_lock_path(first, pr_ref=PR, gate="codex_gate", head_sha=HEAD).name

        assert name == f"proj-one--pr{PR}--codex_gate--{HEAD}.lock"
        assert guard.run_lock_path(second, pr_ref=PR, gate="codex_gate", head_sha=HEAD).name.startswith("proj-two--")

    def test_in_flight_payload_is_no_pass_and_names_the_holder(self, tmp_path):
        state_dir = _state(tmp_path)
        path = guard.run_lock_path(state_dir, pr_ref=PR, gate="codex_gate", head_sha=HEAD)
        proc = _hold_elsewhere(path)
        try:
            payload = guard.in_flight_payload(_acquire(state_dir), pr_number=PR, pr_id="")
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

        from gate_status import decided_verdict, is_pass

        assert payload["status"] == guard.IN_FLIGHT_STATUS and payload["holder_pid"] == proc.pid
        assert is_pass(payload)[0] is False and not decided_verdict(payload)


class TestTheSweep:
    def test_the_sweep_removes_unheld_lock_files_and_keeps_held_ones(self, tmp_path):
        state_dir = _state(tmp_path)
        dead = guard.run_lock_path(state_dir, pr_ref=PR, gate="codex_gate", head_sha=HEAD)
        dead.parent.mkdir(parents=True)
        dead.write_text('{"pid": 999999}', encoding="utf-8")
        live = guard.run_lock_path(state_dir, pr_ref=PR, gate="kimi_gate", head_sha=HEAD)
        proc = _hold_elsewhere(live)
        try:
            removed = guard.sweep_run_locks(state_dir)
            assert live.exists()
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

        assert removed == [dead.name]
        assert not dead.exists()

    def test_a_store_without_run_locks_sweeps_nothing(self, tmp_path):
        assert guard.sweep_run_locks(_state(tmp_path)) == []


class TestTheCount:
    @pytest.mark.parametrize("result", [
        {"status": "unavailable", "reason": "gate_execution_degenerate"},
        {"status": "unavailable", "reason": "timeout"},
        {"status": "unavailable", "reason": "stall"},
        {"status": "failed", "reason": "stalled"},
        {"status": "partial_review", "reason": "diff_truncated"},
    ])
    def test_a_run_in_which_the_reviewer_ran_counts(self, result):
        assert guard.reviewer_ran(result) is True

    @pytest.mark.parametrize("result", [
        {"status": "unavailable", "reason": "lane_exhausted"},
        {"status": "unavailable", "reason": "worktree_checkout_failed"},
        {"status": "unavailable", "reason": "binary_not_found"},
        {"status": "unavailable", "reason": "model_not_supported"},
        {"status": "not_executable", "reason": "provider_not_installed"},
        {"status": "completed", "contract_hash": "h", "report_path": "/r.md"},
        {},
        None,
    ])
    def test_a_refusal_that_ran_no_reviewer_does_not_count(self, result):
        assert guard.reviewer_ran(result) is False

    def test_the_count_starts_again_on_a_new_head(self):
        first = guard.next_head_runs(None, HEAD, {"status": "unavailable", "reason": "timeout"})
        second = guard.next_head_runs(first, HEAD, {"status": "unavailable", "reason": "gate_execution_degenerate"})
        other = guard.next_head_runs(second, "d4" * 20, {"status": "unavailable", "reason": "timeout"})

        assert (first["reviewer_runs"], second["reviewer_runs"], other["reviewer_runs"]) == (1, 2, 1)
        assert second["last_reason"] == "gate_execution_degenerate"
        assert guard.cap_reached(second, HEAD) is True
        assert guard.cap_reached(second, "d4" * 20) is False

    def test_an_unknown_head_is_never_capped(self):
        assert guard.cap_reached({"head_sha": "", "reviewer_runs": 9}, "") is False
        assert guard.runs_on_head({"head_sha": HEAD, "reviewer_runs": "2"}, HEAD) == 0
