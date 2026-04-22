#!/usr/bin/env python3
"""Tests for latency PR-1: dispatcher cooldown + chunk_timeout/total_deadline env tunables.

Covers:
  1. VNX_CHUNK_TIMEOUT env var overrides read_events_with_timeout default
  2. VNX_TOTAL_DEADLINE env var overrides read_events_with_timeout default
  3. VNX_CHUNK_TIMEOUT overrides deliver_via_subprocess default
  4. VNX_TOTAL_DEADLINE overrides deliver_via_subprocess default
  5. New defaults are 300s chunk_timeout and 900s total_deadline
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_blocking_pipe() -> tuple[MagicMock, int]:
    """Create a mock process whose stdout blocks forever."""
    import os as _os
    r_fd, w_fd = _os.pipe()
    proc = MagicMock()
    proc.pid = 99001
    proc.poll.return_value = None
    proc.returncode = None
    proc.stdout = _os.fdopen(r_fd, "rb")
    return proc, w_fd


def _make_pipe_process(lines: list[bytes], pid: int = 99002) -> MagicMock:
    """Create a mock process whose stdout yields lines then EOF."""
    import os as _os
    r_fd, w_fd = _os.pipe()
    with _os.fdopen(w_fd, "wb") as w:
        for line in lines:
            w.write(line)
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    proc.returncode = None
    proc.stdout = _os.fdopen(r_fd, "rb")
    return proc


def _event_line(**fields) -> bytes:
    return json.dumps(fields).encode() + b"\n"


# ---------------------------------------------------------------------------
# 1–2: SubprocessAdapter env var overrides
# ---------------------------------------------------------------------------

class TestChunkTimeoutEnvOverride:
    def test_vnx_chunk_timeout_shortens_default(self, monkeypatch):
        """VNX_CHUNK_TIMEOUT=0.1 causes timeout in 0.1s instead of 300s."""
        from subprocess_adapter import SubprocessAdapter

        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "0.1")
        monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)

        adapter = SubprocessAdapter()
        proc, w_fd = _make_blocking_pipe()
        adapter._processes["T1"] = proc

        t0 = time.time()
        events = list(adapter.read_events_with_timeout("T1", total_deadline=10.0))
        elapsed = time.time() - t0

        assert events == []
        assert elapsed < 2.0, f"Expected fast timeout via env, got {elapsed:.2f}s"
        assert "T1" not in adapter._processes

        try:
            os.close(w_fd)
        except OSError:
            pass

    def test_vnx_chunk_timeout_overrides_kwarg(self, monkeypatch):
        """VNX_CHUNK_TIMEOUT env var takes precedence over the kwarg value."""
        from subprocess_adapter import SubprocessAdapter

        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "0.1")
        monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)

        adapter = SubprocessAdapter()
        proc, w_fd = _make_blocking_pipe()
        adapter._processes["T1"] = proc

        t0 = time.time()
        # Pass a large kwarg — env var should override it
        events = list(adapter.read_events_with_timeout("T1", chunk_timeout=300.0, total_deadline=10.0))
        elapsed = time.time() - t0

        assert events == []
        assert elapsed < 2.0, f"Env var should have shortened timeout, got {elapsed:.2f}s"

        try:
            os.close(w_fd)
        except OSError:
            pass


class TestTotalDeadlineEnvOverride:
    def test_vnx_total_deadline_shortens_default(self, monkeypatch):
        """VNX_TOTAL_DEADLINE=0.3 causes deadline to fire before natural EOF."""
        import threading
        from subprocess_adapter import SubprocessAdapter

        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "0.3")
        monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)

        adapter = SubprocessAdapter()

        import os as _os
        r_fd, w_fd = _os.pipe()
        proc = MagicMock()
        proc.pid = 99003
        proc.poll.return_value = None
        proc.returncode = None
        proc.stdout = _os.fdopen(r_fd, "rb")
        adapter._processes["T1"] = proc

        # Drip events slowly so we exceed the deadline
        def drip():
            try:
                for i in range(20):
                    line = _event_line(type="text", text=f"msg-{i}")
                    _os.write(w_fd, line)
                    time.sleep(0.05)
            except OSError:
                pass
            finally:
                try:
                    _os.close(w_fd)
                except OSError:
                    pass

        t = threading.Thread(target=drip, daemon=True)
        t.start()

        events = list(adapter.read_events_with_timeout("T1", chunk_timeout=10.0))

        # Should have gotten some events but not all 20
        assert len(events) > 0
        assert len(events) < 20
        assert "T1" not in adapter._processes
        t.join(timeout=3)

    def test_invalid_env_var_uses_default(self, monkeypatch):
        """Non-numeric VNX_CHUNK_TIMEOUT falls back to passed kwarg."""
        from subprocess_adapter import SubprocessAdapter

        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "not-a-number")
        monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)

        adapter = SubprocessAdapter()
        proc, w_fd = _make_blocking_pipe()
        adapter._processes["T1"] = proc

        t0 = time.time()
        # The explicit kwarg should govern since env var is invalid
        events = list(adapter.read_events_with_timeout("T1", chunk_timeout=0.1, total_deadline=5.0))
        elapsed = time.time() - t0

        assert events == []
        assert elapsed < 2.0, "Should have timed out via explicit kwarg"

        try:
            os.close(w_fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 5: Default values are 300/900 (not the old 120/600)
# ---------------------------------------------------------------------------

class TestDefaultValues:
    def test_signature_defaults_are_updated(self):
        """read_events_with_timeout defaults are 300s / 900s."""
        import inspect
        from subprocess_adapter import SubprocessAdapter

        sig = inspect.signature(SubprocessAdapter.read_events_with_timeout)
        params = sig.parameters
        assert params["chunk_timeout"].default == 300.0, (
            f"Expected 300.0, got {params['chunk_timeout'].default}"
        )
        assert params["total_deadline"].default == 900.0, (
            f"Expected 900.0, got {params['total_deadline'].default}"
        )

    def test_deliver_via_subprocess_defaults_are_updated(self):
        """deliver_via_subprocess defaults are 300s / 900s."""
        import inspect
        import subprocess_dispatch as sd

        sig = inspect.signature(sd.deliver_via_subprocess)
        params = sig.parameters
        assert params["chunk_timeout"].default == 300.0
        assert params["total_deadline"].default == 900.0

    def test_deliver_with_recovery_defaults_are_updated(self):
        """deliver_with_recovery defaults are 300s / 900s."""
        import inspect
        import subprocess_dispatch as sd

        sig = inspect.signature(sd.deliver_with_recovery)
        params = sig.parameters
        assert params["chunk_timeout"].default == 300.0
        assert params["total_deadline"].default == 900.0


# ---------------------------------------------------------------------------
# 3–4: subprocess_dispatch env var overrides (via signature inspection + unit)
# ---------------------------------------------------------------------------

class TestSubprocessDispatchEnvOverride:
    def test_deliver_via_subprocess_reads_chunk_timeout_env(self, monkeypatch):
        """VNX_CHUNK_TIMEOUT/VNX_TOTAL_DEADLINE env vars are passed to read_events_with_timeout."""
        import subprocess_dispatch as sd

        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "42.5")
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "888.0")

        deliver_result = MagicMock()
        deliver_result.success = True
        deliver_result.terminal_id = "T1"
        deliver_result.dispatch_id = "d-test"
        deliver_result.pane_id = None
        deliver_result.path_used = "subprocess"

        mock_instance = MagicMock()
        mock_instance.deliver.return_value = deliver_result
        mock_instance.read_events_with_timeout.return_value = iter([])
        mock_instance.get_session_id.return_value = None

        with patch("subprocess_dispatch.SubprocessAdapter", return_value=mock_instance), \
             patch("subprocess_dispatch._inject_skill_context", return_value="instr"), \
             patch("subprocess_dispatch._inject_permission_profile", return_value="instr"), \
             patch("subprocess_dispatch._resolve_agent_cwd", return_value=None), \
             patch("subprocess_dispatch._write_manifest", return_value=Path("/tmp/m.json")), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"):
            sd.deliver_via_subprocess("T1", "instruction", "sonnet", "d-test")

        assert mock_instance.read_events_with_timeout.called
        _, kwargs = mock_instance.read_events_with_timeout.call_args
        assert kwargs.get("chunk_timeout") == 42.5, f"Got {kwargs.get('chunk_timeout')}"
        assert kwargs.get("total_deadline") == 888.0, f"Got {kwargs.get('total_deadline')}"
