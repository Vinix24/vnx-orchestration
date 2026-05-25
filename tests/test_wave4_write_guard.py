"""Wave 4 PR-3 — central-install write guard.

In a central install, ``VNX_HOME`` is an immutable, shared, versioned code tree
marked by a ``.vnx-install-mode=central`` file. No project state may ever be
written under it. If path detection mis-fired and collapsed ``PROJECT_ROOT``
onto ``VNX_HOME`` (the Wave 4 install-central bug), the resolver must fail loud
instead of silently writing receipts/dispatches/intelligence into the code tree.

The guard lives in two mirrored places, both exercised here:

  * ``scripts/lib/vnx_paths.sh`` — fires at source time over the final
    ``VNX_DATA_DIR`` / ``VNX_STATE_DIR`` / ``VNX_DISPATCH_DIR`` /
    ``VNX_INTELLIGENCE_DIR`` values.
  * the ``bin/vnx`` inline fallback — same check when ``vnx_paths.sh`` is absent.

The guard is gated on the ``.vnx-install-mode`` marker, so standalone-dev and
embedded layouts (where ``PROJECT_ROOT == VNX_HOME`` is legitimate) are
unaffected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_GUARD_MSG = "cannot write project state under immutable central install"

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
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True
    )
    return path.resolve()


def _make_central_install(tmp_path: Path, marker: bool = True) -> Path:
    """A standalone git repo standing in for ~/.vnx-system/versions/<v>."""
    install = _git_init(tmp_path / "vnx-install")
    (install / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh",
        install / "scripts" / "lib" / "vnx_paths.sh",
    )
    if marker:
        (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    return install


def _clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def _source_paths(install: Path, cwd: Path, extra_env: dict | None = None):
    """Source vnx_paths.sh in a subprocess; return CompletedProcess."""
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{install}/scripts/lib/vnx_paths.sh"; '
            'printf "PROJECT_ROOT=%s\\n" "$PROJECT_ROOT"; '
            'printf "VNX_DATA_DIR=%s\\n" "$VNX_DATA_DIR"',
        ],
        cwd=cwd,
        env=_clean_env(extra_env),
        capture_output=True,
        text=True,
    )


# ── vnx_paths.sh guard ───────────────────────────────────────────────────────
def test_paths_sh_blocks_data_dir_under_vnx_home(tmp_path):
    """VNX_DATA_DIR explicitly pointed inside VNX_HOME → fail loud."""
    install = _make_central_install(tmp_path)
    project = _git_init(tmp_path / "real-project")

    result = _source_paths(
        install, project, extra_env={"VNX_DATA_DIR": str(install / ".vnx-data")}
    )
    assert result.returncode != 0
    assert _GUARD_MSG in result.stderr
    assert "VNX_DATA_DIR" in result.stderr


def test_paths_sh_blocks_state_dir_under_vnx_home(tmp_path):
    install = _make_central_install(tmp_path)
    project = _git_init(tmp_path / "real-project")

    result = _source_paths(
        install, project, extra_env={"VNX_STATE_DIR": str(install / "state")}
    )
    assert result.returncode != 0
    assert _GUARD_MSG in result.stderr


def test_paths_sh_allows_data_dir_in_project(tmp_path):
    """Correct central detection: data lands in the project → no guard."""
    install = _make_central_install(tmp_path)
    project = _git_init(tmp_path / "real-project")

    result = _source_paths(install, project)
    assert result.returncode == 0, result.stderr
    assert _GUARD_MSG not in result.stderr
    resolved = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert resolved["PROJECT_ROOT"] == str(project)
    assert resolved["VNX_DATA_DIR"] == str(project / ".vnx-data")


def test_paths_sh_no_marker_allows_state_under_home(tmp_path):
    """Standalone dev checkout (no marker): state under VNX_HOME is legitimate."""
    install = _make_central_install(tmp_path, marker=False)
    # Run from inside the checkout — standalone dev resolves PROJECT_ROOT=VNX_HOME,
    # so VNX_DATA_DIR lands under VNX_HOME and must NOT trip the guard.
    result = _source_paths(install, install)
    assert result.returncode == 0, result.stderr
    assert _GUARD_MSG not in result.stderr


# ── bin/vnx end-to-end guard ─────────────────────────────────────────────────
def _make_bin_install(tmp_path: Path, with_paths_lib: bool) -> Path:
    install = _git_init(tmp_path / "vnx-install")
    (install / "bin").mkdir()
    shutil.copy(_REPO_ROOT / "bin" / "vnx", install / "bin" / "vnx")
    os.chmod(install / "bin" / "vnx", 0o755)
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    if with_paths_lib:
        (install / "scripts" / "lib").mkdir(parents=True)
        shutil.copy(
            _REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh",
            install / "scripts" / "lib" / "vnx_paths.sh",
        )
    # A skills dir so bootstrap-skills would otherwise have something to copy.
    (install / "skills").mkdir(exist_ok=True)
    (install / "skills" / "skills.yaml").write_text("skills: []\n", encoding="utf-8")
    return install


def test_bin_vnx_bootstrap_skills_blocked_when_cwd_is_install(tmp_path):
    """`cd <central install> && vnx bootstrap-skills` → guard error, exit 1, no write."""
    install = _make_bin_install(tmp_path, with_paths_lib=True)

    result = subprocess.run(
        ["bash", str(install / "bin" / "vnx"), "bootstrap-skills"],
        cwd=install,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert _GUARD_MSG in result.stderr
    # No project state should have been written into the code tree.
    assert not (install / ".claude").exists()


def test_bin_vnx_inline_fallback_blocks_under_home(tmp_path):
    """Inline fallback (no vnx_paths.sh) enforces the same guard."""
    install = _make_bin_install(tmp_path, with_paths_lib=False)
    assert not (install / "scripts" / "lib" / "vnx_paths.sh").exists()

    result = subprocess.run(
        ["bash", str(install / "bin" / "vnx"), "bootstrap-skills"],
        cwd=install,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert _GUARD_MSG in result.stderr
    assert not (install / ".claude").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
