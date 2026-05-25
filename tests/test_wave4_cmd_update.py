"""Wave 4 PR-3 — ``vnx update`` behaviour under a central install.

A central install's shared, versioned code tree is immutable. The mutating
``vnx update`` (clone + install) path must be blocked there with an actionable
redirect to ``install-central.sh`` plus a per-project ``.vnx-version`` repin.
The read-only ``vnx update --check`` dry run must keep working everywhere and
must not mutate anything — in particular it must never touch the project's
``.vnx-version`` pin.

All cases use a local git repo as the update origin so the tests stay offline
and deterministic (no network ``git ls-remote`` / ``git clone``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_VNX_ENV_KEYS = (
    "VNX_HOME",
    "VNX_PROJECT_ROOT",
    "PROJECT_ROOT",
    "VNX_CANONICAL_ROOT",
    "VNX_DATA_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_STATE_DIR",
    "VNX_DISPATCH_DIR",
    "VNX_LOGS_DIR",
    "VNX_PIDS_DIR",
    "VNX_LOCKS_DIR",
    "VNX_SOCKETS_DIR",
    "VNX_REPORTS_DIR",
    "VNX_HEADLESS_REPORTS_DIR",
    "VNX_DB_DIR",
    "VNX_INTELLIGENCE_DIR",
    "VNX_SKILLS_DIR",
)

_PIN = "v1.0.0-rc4\n"


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True
    )
    return path.resolve()


def _make_install(tmp_path: Path, *, marker: bool) -> Path:
    install = _git_init(tmp_path / "vnx-install")
    if marker:
        (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (install / "bin").mkdir()
    shutil.copy(_REPO_ROOT / "bin" / "vnx", install / "bin" / "vnx")
    os.chmod(install / "bin" / "vnx", 0o755)
    (install / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh",
        install / "scripts" / "lib" / "vnx_paths.sh",
    )
    return install


def _make_origin(tmp_path: Path) -> Path:
    """A local git repo standing in for the update origin, with a stub install.sh."""
    origin = _git_init(tmp_path / "origin-repo")
    (origin / "install.sh").write_text(
        "#!/usr/bin/env bash\necho \"[stub-install] target=$1\"\nexit 0\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "install.sh"], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add install"], cwd=origin, check=True)
    # Normalise to 'main' so `git clone --branch main` works regardless of the
    # local git's init.defaultBranch setting.
    subprocess.run(["git", "branch", "-M", "main"], cwd=origin, check=True)
    return origin


def _make_project(tmp_path: Path, name: str = "real-project") -> Path:
    project = _git_init(tmp_path / name)
    (project / ".vnx-version").write_text(_PIN, encoding="utf-8")
    return project


def _run_update(install: Path, project: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    return subprocess.run(
        ["bash", str(install / "bin" / "vnx"), "update", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )


# ── central: --check is a read-only dry run ──────────────────────────────────
def test_central_update_check_succeeds_no_mutation(tmp_path):
    install = _make_install(tmp_path, marker=True)
    origin = _make_origin(tmp_path)
    project = _make_project(tmp_path)

    result = _run_update(install, project, "--check", str(origin))
    assert result.returncode == 0, result.stderr
    assert "Check mode" in result.stdout
    # Dry run: no project runtime dir created, pin untouched.
    assert not (project / ".vnx-data").exists()
    assert (project / ".vnx-version").read_text() == _PIN


def test_central_update_check_preserves_pin(tmp_path):
    install = _make_install(tmp_path, marker=True)
    origin = _make_origin(tmp_path)
    project = _make_project(tmp_path)

    _run_update(install, project, "--check", str(origin))
    assert (project / ".vnx-version").read_text() == _PIN


# ── central: mutating update is blocked + redirected ─────────────────────────
def test_central_update_blocks_self_update(tmp_path):
    install = _make_install(tmp_path, marker=True)
    origin = _make_origin(tmp_path)
    project = _make_project(tmp_path)

    result = _run_update(install, project, str(origin))
    assert result.returncode == 1
    assert "Central install detected" in result.stdout
    assert "install-central.sh" in result.stdout
    # Redirect fires before any clone/install.
    assert "Pulling latest VNX" not in result.stdout


def test_central_update_block_preserves_pin(tmp_path):
    install = _make_install(tmp_path, marker=True)
    origin = _make_origin(tmp_path)
    project = _make_project(tmp_path)

    _run_update(install, project, str(origin))
    assert (project / ".vnx-version").read_text() == _PIN
    # The immutable code tree was not written to.
    assert not (install / ".vnx-origin").exists()


# ── non-central: update proceeds (no redirect) ───────────────────────────────
def test_non_central_update_check_no_redirect(tmp_path):
    install = _make_install(tmp_path, marker=False)
    origin = _make_origin(tmp_path)
    project = _make_project(tmp_path)

    result = _run_update(install, project, "--check", str(origin))
    assert result.returncode == 0, result.stderr
    assert "Central install detected" not in result.stdout
    assert "Check mode" in result.stdout


def test_non_central_full_update_runs_install(tmp_path):
    """Without the central marker, the mutating path proceeds (stub install)."""
    install = _make_install(tmp_path, marker=False)
    origin = _make_origin(tmp_path)
    # Run from inside the checkout: standalone-dev resolves PROJECT_ROOT=VNX_HOME.
    result = _run_update(install, install, str(origin))
    assert "Central install detected" not in result.stdout
    assert result.returncode == 0, result.stderr
    assert "[stub-install]" in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
