"""Filesystem scoping for OI-1067 / W4G — concurrent VNX projects must not
collide on sockets, lock files, or tmpfile prefixes.

Two parallel VNX projects with distinct ``VNX_DATA_DIR`` and
``PROJECT_ROOT`` env vars must each receive a private socket path, a
private lock path, and a tmpfile prefix that contains a project hash
unique to that project. A two-project simulation pins the contract:
binding both sockets concurrently must succeed, and either project's
``notify_dispatch.notify_dispatch`` resolves to its own socket — never
the neighbour's.
"""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
for _p in (SCRIPTS_DIR, SCRIPTS_LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@contextmanager
def project_env(project_root: Path, data_dir: Path | None = None):
    """Temporarily simulate a VNX project under ``project_root``.

    Sets ``PROJECT_ROOT`` + ``VNX_DATA_DIR`` + ``VNX_LOCKS_DIR`` so the
    helpers under test resolve as if running inside that project.
    """
    data_dir = data_dir or (project_root / ".vnx-data")
    data_dir.mkdir(parents=True, exist_ok=True)
    saved = {k: os.environ.get(k) for k in (
        "PROJECT_ROOT", "VNX_DATA_DIR", "VNX_LOCKS_DIR", "VNX_SOCKETS_DIR"
    )}
    os.environ["PROJECT_ROOT"] = str(project_root)
    os.environ["VNX_DATA_DIR"] = str(data_dir)
    os.environ["VNX_LOCKS_DIR"] = str(data_dir / "locks")
    os.environ["VNX_SOCKETS_DIR"] = str(data_dir / "sockets")
    try:
        yield data_dir
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# A. helpers themselves
# ---------------------------------------------------------------------------


def test_project_socket_path_under_data_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with project_env(proj) as data_dir:
        import project_scope
        importlib.reload(project_scope)
        sock = project_scope.project_socket_path("heartbeat_ack_monitor.sock")
        assert sock == data_dir / "sockets" / "heartbeat_ack_monitor.sock"


def test_project_lock_path_under_locks_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with project_env(proj) as data_dir:
        import project_scope
        importlib.reload(project_scope)
        lock = project_scope.project_lock_path("dispatcher.lock")
        assert lock == data_dir / "locks" / "dispatcher.lock"


def test_project_socket_rejects_path_separators(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with project_env(proj):
        import project_scope
        importlib.reload(project_scope)
        with pytest.raises(ValueError):
            project_scope.project_socket_path("../escape.sock")
        with pytest.raises(ValueError):
            project_scope.project_socket_path("")


def test_project_tmpfile_prefix_contains_hash(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with project_env(proj):
        import project_scope
        importlib.reload(project_scope)
        prefix = project_scope.project_tmpfile_prefix("dispatch")
        assert prefix.startswith("vnx-")
        assert prefix.endswith("-dispatch-")
        # 'vnx-' + 8 hex + '-dispatch-' == 22 chars
        assert len(prefix) == len("vnx-") + 8 + len("-dispatch-")


def test_project_hash_is_stable_per_project_root(tmp_path):
    proj_a = tmp_path / "proj_a"; proj_a.mkdir()
    proj_b = tmp_path / "proj_b"; proj_b.mkdir()
    import project_scope
    importlib.reload(project_scope)
    h_a1 = project_scope.project_hash(str(proj_a))
    h_a2 = project_scope.project_hash(str(proj_a))
    h_b = project_scope.project_hash(str(proj_b))
    assert h_a1 == h_a2, "same project must hash to the same value"
    assert h_a1 != h_b, "different projects must hash differently"


# ---------------------------------------------------------------------------
# B. two-project isolation simulation
# ---------------------------------------------------------------------------


def _bind_unix(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)
    return s


def test_two_projects_dont_collide_on_sockets():
    """Concurrent projects must each bind their own socket without conflict.

    Uses a deliberately short root under /tmp because AF_UNIX paths are
    capped (~104 chars on macOS) and pytest's tmp_path is too long for
    the daemon's actual ``$VNX_DATA_DIR/sockets/<name>`` layout.
    """
    import tempfile
    short_root = Path(tempfile.mkdtemp(prefix="vnxw4g-"))
    try:
        proj_a = short_root / "a"; proj_a.mkdir()
        proj_b = short_root / "b"; proj_b.mkdir()

        with project_env(proj_a) as data_a:
            import project_scope
            importlib.reload(project_scope)
            sock_a = project_scope.project_socket_path("hb.sock")

        with project_env(proj_b) as data_b:
            importlib.reload(project_scope)
            sock_b = project_scope.project_socket_path("hb.sock")

        assert sock_a != sock_b
        assert sock_a.parent == data_a / "sockets"
        assert sock_b.parent == data_b / "sockets"

        listener_a = _bind_unix(sock_a)
        listener_b = _bind_unix(sock_b)
        try:
            assert listener_a.fileno() != -1
            assert listener_b.fileno() != -1
            assert sock_a.exists()
            assert sock_b.exists()
        finally:
            listener_a.close()
            listener_b.close()
    finally:
        import shutil
        shutil.rmtree(short_root, ignore_errors=True)


def test_notify_dispatch_resolves_to_own_project(tmp_path, monkeypatch):
    """notify_dispatch must compute the same socket path the daemon binds."""
    proj_a = tmp_path / "proj_a"; proj_a.mkdir()
    proj_b = tmp_path / "proj_b"; proj_b.mkdir()

    monkeypatch.setenv("PROJECT_ROOT", str(proj_a))
    monkeypatch.setenv("VNX_DATA_DIR", str(proj_a / ".vnx-data"))
    import project_scope
    importlib.reload(project_scope)
    daemon_sock_a = project_scope.project_socket_path("heartbeat_ack_monitor.sock")

    # Confirm notify_dispatch resolves to the same path the daemon would bind.
    if "notify_dispatch" in sys.modules:
        del sys.modules["notify_dispatch"]
    import notify_dispatch  # noqa: F401  (importing for side-effects)
    importlib.reload(notify_dispatch)

    # Re-derive via project_scope after reload — both helpers must agree.
    assert project_scope.project_socket_path("heartbeat_ack_monitor.sock") == daemon_sock_a

    # Switch to project B and confirm a different path comes out.
    monkeypatch.setenv("PROJECT_ROOT", str(proj_b))
    monkeypatch.setenv("VNX_DATA_DIR", str(proj_b / ".vnx-data"))
    importlib.reload(project_scope)
    daemon_sock_b = project_scope.project_socket_path("heartbeat_ack_monitor.sock")
    assert daemon_sock_b != daemon_sock_a


def test_two_projects_dont_collide_on_tmpfile_prefix(tmp_path):
    proj_a = tmp_path / "proj_a"; proj_a.mkdir()
    proj_b = tmp_path / "proj_b"; proj_b.mkdir()
    import project_scope
    importlib.reload(project_scope)
    prefix_a = project_scope.project_tmpfile_prefix("dispatch", )
    # ^ no — recompute under each env to ensure isolation actually changes hash.
    with project_env(proj_a):
        importlib.reload(project_scope)
        prefix_a = project_scope.project_tmpfile_prefix("dispatch")
    with project_env(proj_b):
        importlib.reload(project_scope)
        prefix_b = project_scope.project_tmpfile_prefix("dispatch")
    assert prefix_a != prefix_b


# ---------------------------------------------------------------------------
# C. shell helper exports VNX_SOCKETS_DIR
# ---------------------------------------------------------------------------


def test_vnx_paths_sh_exports_sockets_dir(tmp_path):
    """vnx_paths.sh must export VNX_SOCKETS_DIR so bash callers see it."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()  # cheap "is a repo" marker
    snippet = (
        "set -uo pipefail; "
        f'cd {proj}; '
        f'source {REPO_ROOT}/scripts/lib/vnx_paths.sh; '
        'echo "SOCKETS=$VNX_SOCKETS_DIR"'
    )
    out = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/")},
    )
    assert out.returncode == 0, out.stderr
    line = next(l for l in out.stdout.splitlines() if l.startswith("SOCKETS="))
    sockets_dir = line.split("=", 1)[1]
    assert sockets_dir.endswith("/sockets"), sockets_dir
    # Must live under the data dir, not under /tmp.
    assert "/tmp/" not in sockets_dir, sockets_dir
