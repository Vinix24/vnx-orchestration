"""Wave 4 PR-1 — regression tests for the central-install path resolver.

Root cause (issue #225 / Wave 4 synthesis): ``scripts/lib/vnx_paths.sh``,
``scripts/lib/vnx_paths.py`` and the ``bin/vnx`` inline fallback all set
``PROJECT_ROOT = VNX_HOME`` whenever VNX_HOME is its own git repo root. For a
central install (VNX_HOME = ``~/.vnx-system/versions/<v>``) that is wrong: the
operator runs from their own project, so project state, settings and
intelligence ended up inside the immutable code tree.

These tests pin the corrected detection hierarchy:

  1. embedded layout            -> PROJECT_ROOT = vnx_home.parent.parent
  2. VNX_PROJECT_ROOT override  -> PROJECT_ROOT = that dir (shim, belt+braces)
  3. central install (marker)   -> PROJECT_ROOT = CWD git root
  4. central install (heuristic)-> PROJECT_ROOT = CWD git root
  5. standalone dev checkout    -> PROJECT_ROOT = VNX_HOME (unchanged)

Both resolver implementations (Python module + the shell scripts run via
subprocess) are exercised so the three sources cannot drift apart.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import vnx_paths  # noqa: E402
from vnx_paths import (  # noqa: E402
    _default_canonical_root,
    _default_project_root,
    _is_central_install,
    _resolve_project_root,
)

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


def _git_init(path: Path) -> Path:
    """Initialise ``path`` as its own git repository with one empty commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True
    )
    return path.resolve()


@pytest.fixture(autouse=True)
def _clean_vnx_env(monkeypatch):
    """Strip every VNX_* path var so detection is driven only by the test."""
    for key in _VNX_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _make_central_install(tmp_path: Path, marker: bool) -> Path:
    """A standalone git repo standing in for ~/.vnx-system/versions/<v>."""
    install = _git_init(tmp_path / "vnx-install")
    if marker:
        (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    return install


# ── Case 1: embedded layout (unchanged) ──────────────────────────────────────
def test_embedded_layout_resolves_parent_project(tmp_path):
    project = tmp_path / "my-project"
    vnx_home = project / ".claude" / "vnx-system"
    vnx_home.mkdir(parents=True)

    assert _default_project_root(vnx_home.resolve()) == project.resolve()


# ── Case 2: central install + marker file ────────────────────────────────────
def test_central_install_with_marker_uses_cwd_git_root(tmp_path, monkeypatch):
    install = _make_central_install(tmp_path, marker=True)
    project = _git_init(tmp_path / "real-project")

    monkeypatch.chdir(project)
    assert _is_central_install(install) is True
    assert _default_project_root(install) == project
    assert _default_project_root(install) != install


# ── Case 3: central install, no marker, CWD git mismatch ─────────────────────
def test_central_install_detected_by_git_mismatch(tmp_path, monkeypatch):
    install = _make_central_install(tmp_path, marker=False)
    project = _git_init(tmp_path / "real-project")

    monkeypatch.chdir(project)
    assert not (install / ".vnx-install-mode").exists()
    assert _is_central_install(install) is True
    assert _default_project_root(install) == project


# ── Case 4: standalone dev checkout (unchanged) ──────────────────────────────
def test_standalone_dev_checkout_keeps_vnx_home(tmp_path, monkeypatch):
    # VNX_HOME is its own git repo, no marker, and CWD is inside VNX_HOME.
    checkout = _git_init(tmp_path / "vnx-orchestration")

    monkeypatch.chdir(checkout)
    assert _is_central_install(checkout) is False
    assert _default_project_root(checkout) == checkout


# ── Case 5: explicit VNX_PROJECT_ROOT override ───────────────────────────────
def test_vnx_project_root_env_override_wins(tmp_path, monkeypatch):
    install = _make_central_install(tmp_path, marker=True)
    project = _git_init(tmp_path / "real-project")
    override = _git_init(tmp_path / "override-project")

    # CWD points at `project`, but the explicit override must win.
    monkeypatch.chdir(project)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(override))

    assert _default_project_root(install) == override
    assert _resolve_project_root(install) == override


def test_vnx_project_root_ignored_when_equal_to_vnx_home(tmp_path, monkeypatch):
    # A mis-detected override pointing at VNX_HOME must be ignored, not honored.
    install = _make_central_install(tmp_path, marker=False)
    project = _git_init(tmp_path / "real-project")

    monkeypatch.chdir(project)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(install))

    assert _default_project_root(install) == project


# ── Case 6: central install + CWD outside any git repo ───────────────────────
def test_central_install_cwd_outside_git_falls_back_to_cwd(tmp_path, monkeypatch):
    install = _make_central_install(tmp_path, marker=True)
    outside = (tmp_path / "loose-dir")
    outside.mkdir()

    # Precondition: the loose dir is not inside any git repo.
    if vnx_paths._git_toplevel(outside) is not None:
        pytest.skip("temp dir unexpectedly lives inside a git repository")

    monkeypatch.chdir(outside)
    assert _is_central_install(install) is True
    # No git root for CWD -> fall back to CWD itself (never to VNX_HOME).
    assert _default_project_root(install) == outside.resolve()


def test_central_install_refuses_filesystem_root(tmp_path, monkeypatch):
    # Safety guard: if CWD resolves to "/", PROJECT_ROOT must not collapse there.
    install = _make_central_install(tmp_path, marker=True)

    # VNX_HOME still resolves to its own git root (keeps the central branch
    # reachable); only CWD is forced to "/" with no git root above it.
    def _fake_toplevel(p):
        return install if Path(p).resolve() == install else None

    monkeypatch.setattr(vnx_paths.Path, "cwd", staticmethod(lambda: Path("/")))
    monkeypatch.setattr(vnx_paths, "_git_toplevel", _fake_toplevel)

    assert _is_central_install(install) is True
    assert _default_project_root(install) == install.resolve()


# ── Case 7: bin/vnx inline fallback parity with vnx_paths.sh ──────────────────
def _run_shell_resolver(script_path: Path, project_cwd: Path) -> dict:
    """Source vnx_paths.sh and capture the resolved exports."""
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    out = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{script_path}"; '
            'printf "PROJECT_ROOT=%s\\n" "$PROJECT_ROOT"; '
            'printf "VNX_HOME=%s\\n" "$VNX_HOME"; '
            'printf "VNX_CANONICAL_ROOT=%s\\n" "$VNX_CANONICAL_ROOT"',
        ],
        cwd=project_cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
    )


def test_shell_resolver_central_mode(tmp_path):
    install = _make_central_install(tmp_path, marker=True)
    (install / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh",
        install / "scripts" / "lib" / "vnx_paths.sh",
    )
    project = _git_init(tmp_path / "real-project")

    resolved = _run_shell_resolver(install / "scripts" / "lib" / "vnx_paths.sh", project)
    assert resolved["VNX_HOME"] == str(install)
    assert resolved["PROJECT_ROOT"] == str(project)
    assert resolved["VNX_CANONICAL_ROOT"] == str(project)


def test_bin_vnx_inline_fallback_central_mode(tmp_path):
    """The bin/vnx inline fallback (no vnx_paths.sh present) must match."""
    install = _make_central_install(tmp_path, marker=True)
    (install / "bin").mkdir()
    shutil.copy(_REPO_ROOT / "bin" / "vnx", install / "bin" / "vnx")
    os.chmod(install / "bin" / "vnx", 0o755)

    # Stub the dispatched command so the REAL path resolver runs unmodified
    # and we can observe what it computed. No scripts/lib/vnx_paths.sh -> the
    # inline fallback fires.
    cmds = install / "scripts" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "doctor.sh").write_text(
        "cmd_doctor() {\n"
        '  printf "PROJECT_ROOT=%s\\n" "$PROJECT_ROOT"\n'
        '  printf "VNX_HOME=%s\\n" "$VNX_HOME"\n'
        '  printf "VNX_CANONICAL_ROOT=%s\\n" "$VNX_CANONICAL_ROOT"\n'
        "  exit 0\n"
        "}\n",
        encoding="utf-8",
    )
    assert not (install / "scripts" / "lib" / "vnx_paths.sh").exists()

    project = _git_init(tmp_path / "real-project")
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    out = subprocess.run(
        ["bash", str(install / "bin" / "vnx"), "doctor"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    resolved = dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
    )
    assert resolved["VNX_HOME"] == str(install)
    assert resolved["PROJECT_ROOT"] == str(project)
    assert resolved["VNX_CANONICAL_ROOT"] == str(project)


# ── Case 8: VNX_CANONICAL_ROOT follows the project in central mode ───────────
def test_canonical_root_is_project_not_vnx_home_in_central(tmp_path, monkeypatch):
    install = _make_central_install(tmp_path, marker=True)
    project = _git_init(tmp_path / "real-project")

    monkeypatch.chdir(project)
    canonical = _default_canonical_root(install)
    assert canonical == project
    assert canonical != install


def test_resolve_paths_end_to_end_central(tmp_path, monkeypatch):
    """Full resolve_paths(): data + intelligence land in the project, not VNX_HOME."""
    install = _make_central_install(tmp_path, marker=True)
    project = _git_init(tmp_path / "real-project")

    monkeypatch.setenv("VNX_HOME", str(install))
    monkeypatch.chdir(project)

    paths = vnx_paths.resolve_paths()
    assert paths["VNX_HOME"] == str(install)
    assert paths["PROJECT_ROOT"] == str(project)
    assert paths["VNX_DATA_DIR"] == str(project / ".vnx-data")
    assert paths["VNX_INTELLIGENCE_DIR"] == str(project / ".vnx-intelligence")
    # Nothing should resolve inside the immutable code tree.
    assert str(install) not in paths["VNX_DATA_DIR"]
    assert str(install) not in paths["VNX_INTELLIGENCE_DIR"]
