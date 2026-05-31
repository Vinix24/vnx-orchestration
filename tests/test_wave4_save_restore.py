"""Wave 4 PR-3 — bin/vnx save/restore of VNX_PROJECT_ROOT.

The central-install shim exports ``VNX_PROJECT_ROOT`` per invocation. ``bin/vnx``
resets the project-context env vars (``PROJECT_ROOT``/``VNX_HOME``/…) so the
resolver re-detects from a clean slate, but it must preserve the explicit
``VNX_PROJECT_ROOT`` override across that reset and re-export it, so
``vnx_paths.sh`` re-applies it (detection priority 2).

The discriminating test: point CWD at one git project but ``VNX_PROJECT_ROOT``
at a *different* one. If save/restore works, the explicit override wins; if it
were dropped during the unset phase, the resolver would fall back to the CWD
project instead.
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

_DOCTOR_STUB = (
    "cmd_doctor() {\n"
    '  printf "PROJECT_ROOT=%s\\n" "$PROJECT_ROOT"\n'
    '  printf "VNX_HOME=%s\\n" "$VNX_HOME"\n'
    '  printf "VNX_DATA_DIR=%s\\n" "$VNX_DATA_DIR"\n'
    "  exit 0\n"
    "}\n"
)


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True
    )
    return path.resolve()


def _make_install(tmp_path: Path) -> Path:
    install = _git_init(tmp_path / "vnx-install")
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (install / "bin").mkdir()
    shutil.copy(_REPO_ROOT / "bin" / "vnx", install / "bin" / "vnx")
    os.chmod(install / "bin" / "vnx", 0o755)
    (install / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh",
        install / "scripts" / "lib" / "vnx_paths.sh",
    )
    cmds = install / "scripts" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "doctor.sh").write_text(_DOCTOR_STUB, encoding="utf-8")
    return install


def _run_doctor(install: Path, cwd: Path, extra_env: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    if extra_env:
        env.update(extra_env)
    out = subprocess.run(
        ["bash", str(install / "bin" / "vnx"), "doctor"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def test_explicit_override_survives_env_reset(tmp_path):
    """VNX_PROJECT_ROOT (project A) must win over CWD (project B)."""
    install = _make_install(tmp_path)
    override = _git_init(tmp_path / "override-project")
    cwd_project = _git_init(tmp_path / "cwd-project")

    resolved = _run_doctor(
        install, cwd_project, extra_env={"VNX_PROJECT_ROOT": str(override)}
    )
    assert resolved["VNX_HOME"] == str(install)
    # Override survived the unset/restore phase and beat the CWD project.
    assert resolved["PROJECT_ROOT"] == str(override)
    assert resolved["PROJECT_ROOT"] != str(cwd_project)


def test_override_drives_data_dir(tmp_path):
    """Data dir follows the restored override, not the CWD."""
    install = _make_install(tmp_path)
    override = _git_init(tmp_path / "override-project")
    cwd_project = _git_init(tmp_path / "cwd-project")

    resolved = _run_doctor(
        install, cwd_project, extra_env={"VNX_PROJECT_ROOT": str(override)}
    )
    assert resolved["VNX_DATA_DIR"] == str(override / ".vnx-data")


def test_no_override_falls_back_to_cwd(tmp_path):
    """Without VNX_PROJECT_ROOT, central detection uses the CWD git root."""
    install = _make_install(tmp_path)
    cwd_project = _git_init(tmp_path / "cwd-project")

    resolved = _run_doctor(install, cwd_project)
    assert resolved["PROJECT_ROOT"] == str(cwd_project)


def test_override_equal_to_vnx_home_ignored(tmp_path):
    """A bad override pointing at VNX_HOME must be ignored, not honored."""
    install = _make_install(tmp_path)
    cwd_project = _git_init(tmp_path / "cwd-project")

    resolved = _run_doctor(
        install, cwd_project, extra_env={"VNX_PROJECT_ROOT": str(install)}
    )
    # Falls through to central detection → CWD project, never VNX_HOME.
    assert resolved["PROJECT_ROOT"] == str(cwd_project)
    assert resolved["PROJECT_ROOT"] != str(install)


def test_override_preserved_alongside_data_dir_override(tmp_path):
    """VNX_DATA_DIR and VNX_PROJECT_ROOT are both preserved across the reset."""
    install = _make_install(tmp_path)
    override = _git_init(tmp_path / "override-project")
    cwd_project = _git_init(tmp_path / "cwd-project")
    explicit_data = tmp_path / "explicit-data"

    resolved = _run_doctor(
        install,
        cwd_project,
        extra_env={
            "VNX_PROJECT_ROOT": str(override),
            "VNX_DATA_DIR": str(explicit_data),
        },
    )
    assert resolved["PROJECT_ROOT"] == str(override)
    assert resolved["VNX_DATA_DIR"] == str(explicit_data)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
