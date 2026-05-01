#!/usr/bin/env python3
"""OI-1079 regression: notify_dispatch and heartbeat_ack_monitor_daemon must use
project-scoped socket paths so concurrent VNX projects never cross-talk.

Before the fix:
- heartbeat_ack_monitor_daemon.py bound to VNX_DATA_DIR/heartbeat_ack_monitor.sock
- notify_dispatch.py (after W4G) connected to VNX_DATA_DIR/sockets/heartbeat_ack_monitor.sock
This path mismatch meant either the client could not connect, OR both sides used a
shared /tmp path that leaked receipts across VNX-T0 sessions.

After the fix:
- Both daemon and client use project_socket_path("heartbeat_ack_monitor.sock")
  which resolves to VNX_DATA_DIR/sockets/heartbeat_ack_monitor.sock.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SCRIPTS_LIB = SCRIPTS / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS))

from project_scope import project_socket_path


class TestProjectSocketPath:
    """Unit tests: project_socket_path routes through VNX_DATA_DIR/sockets/."""

    def test_socket_in_sockets_subdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        path = project_socket_path("heartbeat_ack_monitor.sock")
        assert path == tmp_path / "sockets" / "heartbeat_ack_monitor.sock"

    def test_different_data_dirs_give_different_paths(self, tmp_path, monkeypatch):
        dir_a = tmp_path / "project-a"
        dir_b = tmp_path / "project-b"
        monkeypatch.setenv("VNX_DATA_DIR", str(dir_a))
        path_a = project_socket_path("heartbeat_ack_monitor.sock")
        monkeypatch.setenv("VNX_DATA_DIR", str(dir_b))
        path_b = project_socket_path("heartbeat_ack_monitor.sock")
        assert path_a != path_b

    def test_rejects_slash_in_name(self):
        with pytest.raises(ValueError, match="bare filename"):
            project_socket_path("sockets/bad.sock")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError):
            project_socket_path("")


class TestNotifyCrossSessionIsolation:
    """Functional: two daemons with different VNX_DATA_DIR must not cross-talk."""

    @staticmethod
    def _start_unix_socket_server(sock_path: Path) -> tuple[list, threading.Event]:
        """Bind a Unix socket server that records received messages."""
        received: list[dict] = []
        ready = threading.Event()
        stop = threading.Event()

        def _serve():
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            if sock_path.exists():
                sock_path.unlink()
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(str(sock_path))
            srv.listen(5)
            srv.settimeout(0.2)
            ready.set()
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                    data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    try:
                        received.append(json.loads(data.decode()))
                    except Exception:
                        pass
                    conn.close()
                except socket.timeout:
                    pass
            srv.close()
            if sock_path.exists():
                sock_path.unlink()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        ready.wait(timeout=2.0)
        return received, stop

    def test_notification_reaches_only_intended_project(self, tmp_path, monkeypatch):
        """Send a notification to project-A's socket; project-B must not receive it."""
        # macOS AF_UNIX path limit is 104 bytes — use very short /tmp paths.
        import tempfile as _tempfile
        base = Path(_tempfile.mkdtemp(prefix="vnx"))
        # Keep dir names short: base is already under /tmp/vnxXXXXXX (≤17 chars)
        dir_a = base / "a"
        dir_b = base / "b"

        # Resolve socket paths for each project
        monkeypatch.setenv("VNX_DATA_DIR", str(dir_a))
        sock_a = project_socket_path("heartbeat_ack_monitor.sock")
        monkeypatch.setenv("VNX_DATA_DIR", str(dir_b))
        sock_b = project_socket_path("heartbeat_ack_monitor.sock")

        assert sock_a != sock_b, "Project sockets must differ by data dir"

        # Start mock server for project-A only
        received_a, stop_a = self._start_unix_socket_server(sock_a)

        # Send notification to project-A
        msg = json.dumps({"action": "track_dispatch", "dispatch_id": "test-123"}).encode()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_a))
        client.sendall(msg)
        client.close()

        time.sleep(0.1)
        stop_a.set()

        # project-B socket doesn't exist — project-B cannot receive anything
        assert not sock_b.exists(), "Project-B socket must not exist (not bound)"
        assert len(received_a) == 1, "Project-A must receive exactly one notification"
        assert received_a[0]["dispatch_id"] == "test-123"


class TestDaemonUsesProjectSocketPath:
    """Verify heartbeat_ack_monitor_daemon binds via project_socket_path."""

    def test_daemon_imports_project_socket_path(self):
        """heartbeat_ack_monitor_daemon must import project_socket_path."""
        import importlib.util
        daemon_path = SCRIPTS / "heartbeat_ack_monitor_daemon.py"
        assert daemon_path.exists(), f"daemon not found at {daemon_path}"
        source = daemon_path.read_text(encoding="utf-8")
        assert "project_socket_path" in source, (
            "heartbeat_ack_monitor_daemon.py must use project_socket_path "
            "(OI-1079: cross-project socket isolation)"
        )

    def test_daemon_does_not_use_bare_data_dir_socket(self):
        """Daemon must not bind to VNX_DATA_DIR/heartbeat_ack_monitor.sock (pre-fix path)."""
        daemon_path = SCRIPTS / "heartbeat_ack_monitor_daemon.py"
        source = daemon_path.read_text(encoding="utf-8")
        # The pre-fix pattern was: data_dir / "heartbeat_ack_monitor.sock"
        # After fix, the path goes through project_socket_path which adds sockets/
        assert 'data_dir / "heartbeat_ack_monitor.sock"' not in source, (
            "Daemon must not use the bare data_dir path (OI-1079 regression)"
        )
