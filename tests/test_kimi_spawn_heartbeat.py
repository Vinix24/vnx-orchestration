"""Tests for the provider-lane worker heartbeat (OI-1044).

PR #1387 (OI-944, OI-1007) wired deterministic silence-detection into the
tmux-spawn lane (FileProgressHeartbeat on the pipe-pane log) and the
subprocess lane (EventStreamHeartbeat on the EventStore stream), but not the
provider lane — kimi, the fleet-default build-worker, had no external
liveness signal a supervisor could observe from outside the process.

Covers:
  - _resolve_heartbeat_log_path: per-dispatch path resolution + path-safety
  - drain_stream raw_tee_path: raw stdout bytes mirrored to a per-dispatch file
  - _heartbeat_monitor_loop: silent worker detected + killed + report written
  - False-positive guard: periodic (read-only) activity keeps the worker alive,
    even across a span far longer than a naive worktree-mutation check would
    tolerate — the calibration scenario from the dispatch (22 minutes of
    Read/Grep-only activity with tool-call gaps up to 209s, well under the
    600s threshold)
  - Concurrency isolation: two dispatches' heartbeats never share a signal, so
    one dying can never mask the other's liveness
  - Liveness is checked on the real OS process (a genuine subprocess is
    spawned and verified dead after the kill), never a pidfile

Gate: 20260805-oi1044-provider-lane-heartbeat
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)
PROVIDER_SPAWNS = str(Path(__file__).resolve().parent.parent / "scripts" / "lib" / "provider_spawns")
if PROVIDER_SPAWNS not in sys.path:
    sys.path.insert(0, PROVIDER_SPAWNS)


def _spawn_real_sleeper() -> subprocess.Popen:
    """A real, killable OS process — mirrors kimi_spawn's own start_new_session=True."""
    return subprocess.Popen(
        ["sleep", "300"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class TestResolveHeartbeatLogPath(unittest.TestCase):
    """Per-dispatch path resolution + the same path-safety guard the tmux
    lane applies to its pipe-pane capture path (_start_pipe_pane)."""

    def test_safe_dispatch_id_resolves_under_logs_conversations(self):
        from provider_spawns.kimi_spawn import _resolve_heartbeat_log_path

        path = _resolve_heartbeat_log_path("20260805-oi1044-test")
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "20260805-oi1044-test.log")
        self.assertEqual(path.parent.name, "conversations")
        self.assertEqual(path.parent.parent.name, "logs")

    def test_path_traversal_dispatch_id_rejected(self):
        from provider_spawns.kimi_spawn import _resolve_heartbeat_log_path

        self.assertIsNone(_resolve_heartbeat_log_path("../../etc/passwd"))

    def test_empty_dispatch_id_rejected(self):
        from provider_spawns.kimi_spawn import _resolve_heartbeat_log_path

        self.assertIsNone(_resolve_heartbeat_log_path(""))

    def test_shell_metacharacter_dispatch_id_rejected(self):
        from provider_spawns.kimi_spawn import _resolve_heartbeat_log_path

        self.assertIsNone(_resolve_heartbeat_log_path("d1; rm -rf /"))

    def test_two_dispatches_resolve_to_distinct_files(self):
        """Structural proof of per-dispatch isolation (not per-terminal, not
        the shared ~/.kimi/logs/kimi.log): two dispatch_ids never collide."""
        from provider_spawns.kimi_spawn import _resolve_heartbeat_log_path

        path_a = _resolve_heartbeat_log_path("dispatch-alpha")
        path_b = _resolve_heartbeat_log_path("dispatch-beta")
        self.assertNotEqual(path_a, path_b)


class TestWriteHeartbeatReport(unittest.TestCase):
    def test_report_written_atomically_no_tmp_left_behind(self):
        from provider_spawns.kimi_spawn import _write_heartbeat_report
        from vnx_paths import resolve_paths

        _write_heartbeat_report("dispatch-atomic-001", "## Summary\ncontent\n")

        data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
        report_path = data_dir / "unified_reports" / "dispatch-atomic-001.md"
        tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")

        self.assertTrue(report_path.exists())
        self.assertFalse(tmp_path.exists())
        self.assertEqual(report_path.read_text(encoding="utf-8"), "## Summary\ncontent\n")

    def test_report_passes_body_contract(self):
        """The heartbeat's own report must satisfy validate_body, so
        emit_unified_report's idempotent early-return preserves it untouched
        instead of treating it as a partial draft to replace."""
        from provider_spawns.kimi_spawn import _write_heartbeat_report
        from report_body_contract import validate_body
        from worker_heartbeat import SilenceVerdict, build_heartbeat_failure_report

        verdict = SilenceVerdict(is_silent=True, silence_seconds=650.0, threshold_seconds=600.0)
        report = build_heartbeat_failure_report(
            "dispatch-contract-001", verdict, model="kimi-k2.6", provider="kimi", terminal_id="T1",
        )
        _write_heartbeat_report("dispatch-contract-001", report)

        from vnx_paths import resolve_paths
        data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
        report_path = data_dir / "unified_reports" / "dispatch-contract-001.md"
        result = validate_body(report_path.read_text(encoding="utf-8"))
        self.assertTrue(result.valid, f"heartbeat report failed contract: {result}")


class TestDrainStreamRawTee(unittest.TestCase):
    """drain_stream's raw_tee_path mirrors subprocess stdout bytes to a file —
    the same interception point the tmux lane's pipe-pane capture uses, but
    for the provider lane's raw Popen pipe instead of a tmux pane."""

    def test_bytes_mirrored_to_tee_file(self):
        from _streaming_drainer import StreamingDrainerMixin
        from canonical_event import CanonicalEvent

        class _Host(StreamingDrainerMixin):
            provider_name = "kimi"

            def _normalize(self, raw):
                return CanonicalEvent(
                    dispatch_id="d1", terminal_id="T1", provider="kimi",
                    event_type="text", data={"text": raw.get("content", "")},
                    observability_tier=1,
                )

        payload = b'{"content": "hello"}\n{"content": "world"}\n'
        read_fd, write_fd = os.pipe()

        def _writer():
            os.write(write_fd, payload)
            os.close(write_fd)

        threading.Thread(target=_writer, daemon=True).start()

        proc = MagicMock()
        proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
        proc.poll.return_value = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            tee_path = Path(tmpdir) / "conversations" / "d1.log"
            host = _Host()
            events = list(host.drain_stream(
                proc, "T1", "d1", None,
                chunk_timeout=5.0, total_deadline=5.0, raw_tee_path=tee_path,
            ))
            self.assertEqual(len(events), 2)
            self.assertTrue(tee_path.exists())
            self.assertEqual(tee_path.read_bytes(), payload)

    def test_no_tee_path_is_a_no_op(self):
        """Default (raw_tee_path=None) — existing codex/gemini/litellm callers
        that never pass this param see zero behavior change."""
        from _streaming_drainer import StreamingDrainerMixin
        from canonical_event import CanonicalEvent

        class _Host(StreamingDrainerMixin):
            provider_name = "kimi"

            def _normalize(self, raw):
                return CanonicalEvent(
                    dispatch_id="d1", terminal_id="T1", provider="kimi",
                    event_type="text", data={"text": raw.get("content", "")},
                    observability_tier=1,
                )

        payload = b'{"content": "hi"}\n'
        read_fd, write_fd = os.pipe()

        def _writer():
            os.write(write_fd, payload)
            os.close(write_fd)

        threading.Thread(target=_writer, daemon=True).start()

        proc = MagicMock()
        proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
        proc.poll.return_value = 0

        host = _Host()
        events = list(host.drain_stream(proc, "T1", "d1", None, chunk_timeout=5.0, total_deadline=5.0))
        self.assertEqual(len(events), 1)


class TestHeartbeatMonitorLoopKillsSilentWorker(unittest.TestCase):
    """The core OI-1044 scenario: a stuck provider-lane worker gets detected
    and killed within the threshold, by a signal external to the process."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "0.2"}
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)

    def tearDown(self):
        self._tmpdir.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_silent_worker_killed_and_reported_within_threshold(self):
        from provider_spawns.kimi_spawn import _heartbeat_monitor_loop
        from vnx_paths import resolve_paths

        proc = _spawn_real_sleeper()
        pid = proc.pid
        self.assertTrue(_pid_alive(pid), "precondition: sleeper must start alive")

        # Empty tee file that never grows — the silent-worker scenario.
        log_path = Path(self._tmpdir.name) / "silent.log"
        log_path.write_text("")

        stop_event = threading.Event()
        killed_event = threading.Event()
        thread = threading.Thread(
            target=_heartbeat_monitor_loop,
            args=(proc, log_path, "dispatch-silent-001", "T1", "kimi-k2.6", stop_event, killed_event),
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        try:
            fired = killed_event.wait(timeout=5.0)
            self.assertTrue(fired, "heartbeat did not fire within 5s for a 0.2s threshold")

            # Item #2 (OI-1044): liveness must be verified on the real OS
            # process, never a pidfile — prove the kernel actually reaped it.
            deadline = time.monotonic() + 3.0
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_pid_alive(pid), "worker process must actually be dead, not just flagged")

            data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
            report_path = data_dir / "unified_reports" / "dispatch-silent-001.md"
            self.assertTrue(report_path.exists(), "heartbeat must write the failure report directly")
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("## Summary", report_text)
            self.assertIn("## Changes", report_text)
            self.assertIn("## Verification", report_text)
            self.assertIn("## Open Items", report_text)
            self.assertIn("dispatch-silent-001", report_text)
            self.assertIn("kimi", report_text)
        finally:
            stop_event.set()
            thread.join(timeout=2.0)
            if _pid_alive(pid):
                proc.kill()
                proc.wait(timeout=2.0)


class TestHeartbeatKilledEventSetAtKillDecision(unittest.TestCase):
    """OI-1082: killed_event must be set at the kill DECISION, before
    _kill_process (worst-case ~10s SIGTERM+SIGKILL waits) and the report
    write. spawn_kimi joins the monitor thread with a 5s cap and then reads
    the event — setting it last meant a slow kill left the flag reading False
    after a real kill, downgrading the receipt's failure_reason to a generic
    'kimi exited with code -9'."""

    def setUp(self):
        self._env_patch = {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "0.2"}
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_killed_event_visible_while_kill_still_in_flight(self):
        import provider_spawns.kimi_spawn as kimi_spawn_mod

        # Silent tee file that exists but never grows.
        log_path = Path(self._tmpdir.name) / "silent.log"
        log_path.write_text("")

        kill_started = threading.Event()
        release_kill = threading.Event()

        def _slow_kill(proc):
            # Models a worker that ignores SIGTERM: _kill_process blocks in its
            # SIGTERM-wait + SIGKILL-wait windows (~10s worst case).
            kill_started.set()
            release_kill.wait(timeout=5.0)

        stop_event = threading.Event()
        killed_event = threading.Event()
        proc = MagicMock()

        with patch.object(kimi_spawn_mod, "_kill_process", side_effect=_slow_kill), \
             patch.object(kimi_spawn_mod, "_write_heartbeat_report"):
            thread = threading.Thread(
                target=kimi_spawn_mod._heartbeat_monitor_loop,
                args=(proc, log_path, "dispatch-oi1082-race", "T1", "kimi-k3",
                      stop_event, killed_event),
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            thread.start()
            try:
                self.assertTrue(
                    kill_started.wait(timeout=5.0),
                    "precondition: heartbeat must reach its kill decision",
                )
                # The kill is still blocked in _slow_kill. This is exactly the
                # window in which spawn_kimi's 5s-capped join expires and reads
                # the flag — it must already be True HERE.
                self.assertTrue(
                    killed_event.wait(timeout=1.0),
                    "killed_event must be set at kill-decision time, not after "
                    "the kill + report write complete",
                )
            finally:
                release_kill.set()
                stop_event.set()
                thread.join(timeout=3.0)


class TestHeartbeatMonitorLoopFalsePositiveGuard(unittest.TestCase):
    """The calibration finding that MUST NOT regress: a worker producing
    periodic read-only tool-call activity (no file writes, but a steady
    trickle of stdout bytes — Read/Grep/Bash tool_call+tool_result pairs)
    must NOT be killed, even across a span exceeding what a naive
    worktree-mutation-only check would tolerate."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "0.3"}
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)

    def tearDown(self):
        self._tmpdir.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_periodic_activity_within_threshold_never_killed(self):
        from provider_spawns.kimi_spawn import _heartbeat_monitor_loop

        proc = _spawn_real_sleeper()
        log_path = Path(self._tmpdir.name) / "active.log"
        log_path.write_text("")

        stop_event = threading.Event()
        killed_event = threading.Event()
        thread = threading.Thread(
            target=_heartbeat_monitor_loop,
            args=(proc, log_path, "dispatch-active-001", "T1", "kimi-k2.6", stop_event, killed_event),
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        try:
            # Simulate a worker whose gaps between tool calls (0.1s) stay well
            # under the 0.3s threshold — the scaled-down analogue of kimi's
            # measured <=209s inter-step gaps against the 600s default.
            deadline = time.monotonic() + 1.0
            with open(log_path, "ab", buffering=0) as fh:
                while time.monotonic() < deadline:
                    fh.write(b'{"type":"tool_call"}\n')
                    time.sleep(0.1)

            self.assertFalse(
                killed_event.is_set(),
                "false positive: a worker with sub-threshold activity gaps must not be killed",
            )
            self.assertTrue(_pid_alive(proc.pid), "process must still be running")
        finally:
            stop_event.set()
            thread.join(timeout=2.0)
            if _pid_alive(proc.pid):
                proc.kill()
                proc.wait(timeout=2.0)


class TestHeartbeatConcurrencyIsolation(unittest.TestCase):
    """Two concurrent dispatches must never mask each other's liveness — the
    exact failure mode of the shared ~/.kimi/logs/kimi.log this design avoids."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "0.2"}
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)

    def tearDown(self):
        self._tmpdir.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_dead_dispatch_does_not_mask_live_dispatch(self):
        from provider_spawns.kimi_spawn import _heartbeat_monitor_loop

        proc_dead = _spawn_real_sleeper()
        proc_alive = _spawn_real_sleeper()

        log_dead = Path(self._tmpdir.name) / "dead.log"
        log_dead.write_text("")
        log_alive = Path(self._tmpdir.name) / "alive.log"
        log_alive.write_text("")

        stop_dead, killed_dead = threading.Event(), threading.Event()
        stop_alive, killed_alive = threading.Event(), threading.Event()

        t_dead = threading.Thread(
            target=_heartbeat_monitor_loop,
            args=(proc_dead, log_dead, "dispatch-dead-001", "T1", "kimi-k2.6", stop_dead, killed_dead),
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        t_alive = threading.Thread(
            target=_heartbeat_monitor_loop,
            args=(proc_alive, log_alive, "dispatch-alive-001", "T2", "kimi-k2.6", stop_alive, killed_alive),
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        t_dead.start()
        t_alive.start()
        try:
            deadline = time.monotonic() + 1.5
            with open(log_alive, "ab", buffering=0) as fh:
                while time.monotonic() < deadline:
                    fh.write(b'{"type":"tool_call"}\n')
                    time.sleep(0.05)

            self.assertTrue(killed_dead.wait(timeout=3.0), "the genuinely stuck dispatch must still be killed")
            self.assertFalse(
                killed_alive.is_set(),
                "a live, concurrently-running dispatch must not be killed by another dispatch's silence",
            )
            self.assertFalse(_pid_alive(proc_dead.pid))
            self.assertTrue(_pid_alive(proc_alive.pid))
        finally:
            stop_dead.set()
            stop_alive.set()
            t_dead.join(timeout=2.0)
            t_alive.join(timeout=2.0)
            for p in (proc_dead, proc_alive):
                if _pid_alive(p.pid):
                    p.kill()
                    p.wait(timeout=2.0)


class TestSpawnKimiHeartbeatWiring(unittest.TestCase):
    """spawn_kimi() itself starts/stops the monitor thread and surfaces a
    heartbeat kill as a diagnosable result.error — without needing to wait
    out the real 30s poll interval in a test."""

    def test_heartbeat_thread_started_and_stopped_around_drain(self):
        """Real pipe (not a bare BytesIO) so the drainer's own producer thread
        also runs concurrently — proving the heartbeat thread is tracked
        specifically by name, not just "whichever thread was created last"."""
        from provider_spawns import kimi_spawn

        heartbeat_threads = []
        real_thread_cls = threading.Thread

        class _RecordingThread(real_thread_cls):
            def start(self):
                if isinstance(self._args and self._args[0], object) and str(self.name).startswith("kimi-heartbeat-"):
                    heartbeat_threads.append(self)
                super().start()

        payload = json.dumps({"event_type": "complete"}).encode() + b"\n"
        read_fd, write_fd = os.pipe()

        def _writer():
            os.write(write_fd, payload)
            os.close(write_fd)

        threading.Thread(target=_writer, daemon=True).start()

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.poll.return_value = 0
        fake_proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
        fake_proc.stderr = io.BytesIO(b"")
        fake_proc.wait = MagicMock(return_value=0)

        with patch.object(kimi_spawn, "_start_kimi_subprocess", return_value=(fake_proc, None)), \
             patch("threading.Thread", _RecordingThread):
            kimi_spawn.spawn_kimi("prompt", dispatch_id="dispatch-wiring-001", terminal_id="T1")

        self.assertEqual(len(heartbeat_threads), 1, "exactly one heartbeat monitor thread must be started")
        self.assertFalse(heartbeat_threads[0].is_alive(), "heartbeat thread must be stopped+joined before spawn_kimi returns")

    def test_unsafe_dispatch_id_skips_heartbeat_without_crashing(self):
        """When _resolve_heartbeat_log_path returns None, spawn_kimi must still
        complete normally — a missing heartbeat is fail-open, not fail-closed."""
        from provider_spawns import kimi_spawn

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.poll.return_value = 0
        fake_proc.stdout = io.BytesIO(json.dumps({"event_type": "complete"}).encode() + b"\n")
        fake_proc.stderr = io.BytesIO(b"")
        fake_proc.wait = MagicMock(return_value=0)

        with patch.object(kimi_spawn, "_start_kimi_subprocess", return_value=(fake_proc, None)):
            result = kimi_spawn.spawn_kimi("prompt", dispatch_id="../unsafe", terminal_id="T1")

        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0)

    def test_heartbeat_kill_overrides_result_error_with_diagnosable_message(self):
        """When the monitor thread sets killed_event, spawn_kimi must replace
        the generic 'kimi exited with code N' with a message that names the
        heartbeat and points at the report the monitor already wrote."""
        from provider_spawns import kimi_spawn

        fake_proc = MagicMock()
        fake_proc.returncode = -9
        fake_proc.poll.return_value = -9
        fake_proc.stdout = io.BytesIO(b"")
        fake_proc.stderr = io.BytesIO(b"")
        fake_proc.wait = MagicMock(return_value=-9)

        def _fake_monitor_loop(proc, log_path, dispatch_id, terminal_id, model, stop_event, killed_event, poll_interval=30.0):
            # Simulate an immediate silence-kill instead of waiting out a real threshold.
            killed_event.set()
            stop_event.wait()

        with patch.object(kimi_spawn, "_start_kimi_subprocess", return_value=(fake_proc, None)), \
             patch.object(kimi_spawn, "_heartbeat_monitor_loop", _fake_monitor_loop):
            result = kimi_spawn.spawn_kimi("prompt", dispatch_id="dispatch-hbkill-001", terminal_id="T1")

        self.assertIsNotNone(result.error)
        self.assertIn("heartbeat", result.error)
        self.assertIn("dispatch-hbkill-001", result.error)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
