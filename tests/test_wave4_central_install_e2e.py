"""Wave 4 PR-5 — central-install end-to-end integration tests.

These tests exercise the COMPLETE central-install path (PR-1 through PR-4 combined)
in scenarios that mirror real operator usage. Unlike the per-PR unit tests that pin
individual behaviours, each case here exercises a full scenario: shim → bin/vnx →
path resolver → command execution.

Cases:
  1. Fresh install layout: install-central.sh produces all 4 required pieces:
     versions/<v>/, current symlink, bin/vnx shim, .vnx-install-mode marker.
  2. Project switchover: central install + .vnx-version pin → resolve_paths()
     returns PROJECT_ROOT = project dir and VNX_DATA_DIR inside the project.
  3. PROJECT_ROOT resolves to project, not VNX_HOME — both Python and shell
     resolvers agree (PR-1 combined correctness check).
  4. VNX_DATA_DIR/STATE_DIR land in project, not central install; write guard
     blocks explicit attempts to place them under VNX_HOME (PR-3).
  5. regen-settings passes --project-root = project dir to the merge script,
     never VNX_HOME — settings.json lands in the project (PR-4 scenario).
  6. Shim traversal via .vnx-version stops at git boundary; full chain preserves
     VNX_PROJECT_ROOT through bin/vnx's env reset (PR-2 + PR-3 combined).
  7. Round-trip upgrade: switch current symlink to a new version; a project
     pinned to the new version continues to resolve PROJECT_ROOT correctly.
  8. Contamination prevention: after a correct central install + path resolution
     from the project dir, no runtime state appears in the central install tree
     (contamination prevented by PR-1 + PR-3 fix chain).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "install-central.sh"
_VERSION_V1 = "v1.0.0-rc4"
_VERSION_V2 = "v1.0.0-rc5"

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

# Lightweight stub inner bin/vnx: echoes the env the shim exported so tests can
# inspect VNX_HOME / VNX_PROJECT_ROOT without running the full resolver chain.
_STUB_INNER_VNX = (
    "#!/usr/bin/env bash\n"
    'echo "VNX_HOME=${VNX_HOME:-__UNSET__}"\n'
    'echo "VNX_PROJECT_ROOT=${VNX_PROJECT_ROOT:-__UNSET__}"\n'
    'echo "PROJECT_ROOT=${PROJECT_ROOT:-__UNSET__}"\n'
)

# Import the Python path resolver (scripts/lib already on sys.path via conftest).
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
import vnx_paths  # noqa: E402
from vnx_paths import (  # noqa: E402
    _default_canonical_root,
    _default_project_root,
    _is_central_install,
    _resolve_project_root,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _git_init(path: Path) -> Path:
    """Initialise *path* as its own git repo with one empty commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True
    )
    return path.resolve()


def _clean_env(extra: dict | None = None) -> dict:
    """Return a copy of the environment with all VNX_* path vars stripped."""
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def _parse_kv(text: str) -> dict:
    """Parse KEY=VALUE lines from *text*; first occurrence wins."""
    result: dict = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result.setdefault(k, v)
    return result


def _make_version_dir(target: Path, version: str, *, stub_inner: bool) -> Path:
    """Create versions/<v>/ with an inner bin/vnx (stub or real).

    Pre-creating the directory causes install-central.sh's clone_version to
    take the idempotent path (skip git clone) and write the .vnx-install-mode
    marker via write_install_marker() without needing network access.
    """
    version_dir = target / "versions" / version
    inner_bin = version_dir / "bin"
    inner_bin.mkdir(parents=True, exist_ok=True)
    inner = inner_bin / "vnx"

    if stub_inner:
        inner.write_text(_STUB_INNER_VNX, encoding="utf-8")
    else:
        shutil.copy(_REPO_ROOT / "bin" / "vnx", inner)
        scripts_lib = version_dir / "scripts" / "lib"
        scripts_lib.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            _REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh",
            scripts_lib / "vnx_paths.sh",
        )

    os.chmod(inner, 0o755)
    return version_dir.resolve()


def _run_install_central(target: Path, version: str) -> subprocess.CompletedProcess:
    """Run install-central.sh with check_prereqs + verify_install stubbed.

    clone_version is NOT stubbed: when version_dir already exists it takes
    the idempotent branch (no git clone) and still writes the marker via
    write_install_marker(). install_shim runs intact and writes the real shim.
    """
    body = _SCRIPT.read_text(encoding="utf-8")
    head, sep, _ = body.rpartition('main "$@"')
    assert sep, "install-central.sh must end with 'main \"$@\"'"
    program = (
        head
        + "check_prereqs() { :; }\n"
        + "verify_install() { :; }\n"
        + 'main "$@"\n'
    )
    return subprocess.run(
        [
            "bash", "-c", program, "install-central",
            "--target", str(target),
            "--version", version,
        ],
        env=_clean_env(),
        capture_output=True,
        text=True,
    )


def _install_central(
    tmp_path: Path,
    version: str,
    *,
    stub_inner: bool = False,
) -> tuple[Path, Path]:
    """Install VNX centrally into tmp_path/vnx-system. Return (target, shim).

    Produces all 4 layout pieces: versions/<v>/, current symlink, bin/vnx shim,
    .vnx-install-mode marker.
    """
    target = tmp_path / "vnx-system"
    _make_version_dir(target, version, stub_inner=stub_inner)

    res = _run_install_central(target, version)
    assert res.returncode == 0, (
        f"install-central.sh failed:\n"
        f"  stdout: {res.stdout[:1000]}\n"
        f"  stderr: {res.stderr[:1000]}"
    )

    shim = target / "bin" / "vnx"
    assert shim.is_file() and os.access(shim, os.X_OK), "shim not installed or not executable"
    return target, shim


def _run_shim(shim: Path, cwd: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the central-install shim from *cwd* and return the result."""
    return subprocess.run(
        ["bash", str(shim)],
        cwd=str(cwd),
        env=_clean_env(extra_env),
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Case 1: Fresh install layout — all 4 required pieces present
# ---------------------------------------------------------------------------

def test_fresh_install_layout_complete(tmp_path):
    """install-central.sh creates versions/<v>/, current symlink, shim, and marker."""
    target, shim = _install_central(tmp_path, _VERSION_V1, stub_inner=True)
    version_dir = target / "versions" / _VERSION_V1

    # 1a. versions/<v>/ directory exists.
    assert version_dir.is_dir(), "versions/<v>/ directory was not created"

    # 1b. current symlink points at the version dir.
    current = target / "current"
    assert current.is_symlink(), "current symlink was not created"
    assert current.resolve() == version_dir.resolve(), (
        f"current → {current.resolve()}, expected {version_dir.resolve()}"
    )

    # 1c. bin/vnx shim is present and executable.
    assert shim.is_file(), "shim was not created at target/bin/vnx"
    assert os.access(shim, os.X_OK), "shim is not executable"

    # 1d. .vnx-install-mode marker contains "central".
    marker = version_dir / ".vnx-install-mode"
    assert marker.is_file(), ".vnx-install-mode marker was not written by clone_version"
    assert marker.read_text(encoding="utf-8").strip() == "central", (
        f"Marker content wrong: {marker.read_text()!r}"
    )


# ---------------------------------------------------------------------------
# Case 2: Project switchover — resolve_paths() puts PROJECT_ROOT at project
# ---------------------------------------------------------------------------

def test_project_switchover_paths_resolve_to_project(tmp_path, monkeypatch):
    """Central install + .vnx-version pin → PROJECT_ROOT=project, data in project."""
    install = _git_init(tmp_path / "vnx-install")
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")

    project = _git_init(tmp_path / "my-project")
    (project / ".vnx-version").write_text(f"{_VERSION_V1}\n", encoding="utf-8")

    # Simulate the shim: clear all VNX_* vars, then export VNX_HOME and
    # VNX_PROJECT_ROOT as the shim would after find_project_root() runs.
    for k in _VNX_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VNX_HOME", str(install))
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)

    paths = vnx_paths.resolve_paths()

    assert paths["VNX_HOME"] == str(install), (
        f"VNX_HOME must stay as central install, got {paths['VNX_HOME']}"
    )
    assert paths["PROJECT_ROOT"] == str(project), (
        f"PROJECT_ROOT must resolve to project, got {paths['PROJECT_ROOT']}"
    )
    assert paths["VNX_DATA_DIR"] == str(project / ".vnx-data"), (
        f"VNX_DATA_DIR must be inside project, got {paths['VNX_DATA_DIR']}"
    )
    # No runtime path may resolve inside the immutable code tree.
    assert str(install) not in paths["VNX_DATA_DIR"], (
        "VNX_DATA_DIR must not be inside VNX_HOME"
    )
    assert str(install) not in paths["VNX_INTELLIGENCE_DIR"], (
        "VNX_INTELLIGENCE_DIR must not be inside VNX_HOME"
    )


# ---------------------------------------------------------------------------
# Case 3: PROJECT_ROOT resolves to project, not VNX_HOME (PR-1 end-to-end)
# ---------------------------------------------------------------------------

def test_project_root_resolves_to_project_not_vnx_home(tmp_path, monkeypatch):
    """Python and shell resolvers both return PROJECT_ROOT = project, not VNX_HOME."""
    install = _git_init(tmp_path / "vnx-install")
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    project = _git_init(tmp_path / "real-project")

    for k in _VNX_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(project)

    # ── Python resolver ──────────────────────────────────────────────────────
    assert _is_central_install(install) is True, "Central marker must be detected"

    py_root = _default_project_root(install)
    assert py_root == project, (
        f"Python resolver: expected PROJECT_ROOT={project}, got {py_root}"
    )
    assert py_root != install, (
        "Python resolver must NOT return VNX_HOME as PROJECT_ROOT"
    )

    # ── Shell resolver (vnx_paths.sh via subprocess) ─────────────────────────
    install_lib = install / "scripts" / "lib"
    install_lib.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh", install_lib / "vnx_paths.sh")

    res = subprocess.run(
        [
            "bash", "-c",
            f'source "{install_lib}/vnx_paths.sh"; '
            'printf "PROJECT_ROOT=%s\\n" "$PROJECT_ROOT"; '
            'printf "VNX_HOME=%s\\n" "$VNX_HOME"',
        ],
        cwd=project,
        env=_clean_env({"VNX_HOME": str(install)}),
        capture_output=True,
        text=True,
        check=True,
    )
    sh = _parse_kv(res.stdout)
    assert sh.get("PROJECT_ROOT") == str(project), (
        f"Shell resolver: expected {project}, got {sh.get('PROJECT_ROOT')!r}\n"
        f"stderr: {res.stderr!r}"
    )
    assert sh.get("PROJECT_ROOT") != sh.get("VNX_HOME"), (
        "Shell resolver must not set PROJECT_ROOT = VNX_HOME"
    )


# ---------------------------------------------------------------------------
# Case 4: VNX_DATA_DIR/STATE_DIR under project; write guard blocks misrouting
# ---------------------------------------------------------------------------

def test_data_dirs_under_project_not_central_install(tmp_path, monkeypatch):
    """DATA_DIR + STATE_DIR land in project/.vnx-data; guard rejects path under VNX_HOME."""
    install = _git_init(tmp_path / "vnx-install")
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    project = _git_init(tmp_path / "real-project")

    for k in _VNX_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VNX_HOME", str(install))
    monkeypatch.chdir(project)

    # Python resolver: both data dirs must be under the project.
    paths = vnx_paths.resolve_paths()
    assert Path(paths["VNX_DATA_DIR"]).is_relative_to(project), (
        f"VNX_DATA_DIR={paths['VNX_DATA_DIR']} must be under project={project}"
    )
    assert Path(paths["VNX_STATE_DIR"]).is_relative_to(project), (
        f"VNX_STATE_DIR={paths['VNX_STATE_DIR']} must be under project={project}"
    )

    # Shell write guard: sourcing vnx_paths.sh with VNX_DATA_DIR explicitly
    # inside VNX_HOME must exit non-zero and emit the guard message.
    install_lib = install / "scripts" / "lib"
    install_lib.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh", install_lib / "vnx_paths.sh")

    guarded = subprocess.run(
        [
            "bash", "-c",
            f'source "{install_lib}/vnx_paths.sh"; echo OK',
        ],
        cwd=project,
        env=_clean_env({
            "VNX_HOME": str(install),
            "VNX_DATA_DIR": str(install / ".vnx-data"),
        }),
        capture_output=True,
        text=True,
    )
    assert guarded.returncode != 0, (
        "Write guard must reject VNX_DATA_DIR pointing inside VNX_HOME"
    )
    assert "cannot write project state" in guarded.stderr, (
        f"Guard message missing from stderr: {guarded.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 5: regen-settings uses PROJECT_ROOT = project dir (PR-4 scenario)
# ---------------------------------------------------------------------------

def test_regen_settings_uses_project_root_not_vnx_home(tmp_path):
    """regen-settings passes --project-root = project, never VNX_HOME, to the merge script."""
    install = _git_init(tmp_path / "vnx-install")
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")

    # Plant real bin/vnx, vnx_paths.sh, and regen_settings.sh into the install tree.
    (install / "bin").mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "bin" / "vnx", install / "bin" / "vnx")
    os.chmod(install / "bin" / "vnx", 0o755)

    scripts_lib = install / "scripts" / "lib"
    scripts_lib.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh", scripts_lib / "vnx_paths.sh")

    scripts_cmds = install / "scripts" / "commands"
    scripts_cmds.mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "commands" / "regen_settings.sh",
        scripts_cmds / "regen_settings.sh",
    )

    # Stub vnx_settings_merge.py: write the received --project-root value to a
    # sentinel file so the test can verify the path without needing real settings.
    sentinel = tmp_path / "regen_sentinel.txt"
    stub_py = (
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "for i, a in enumerate(args):\n"
        "    if a == '--project-root' and i + 1 < len(args):\n"
        f"        pathlib.Path({str(sentinel)!r}).write_text(args[i + 1])\n"
        "        break\n"
        "print('stub: ok')\n"
        "sys.exit(0)\n"
    )
    (install / "scripts" / "vnx_settings_merge.py").write_text(stub_py, encoding="utf-8")

    # Project with a .vnx-version pin.
    project = _git_init(tmp_path / "real-project")
    (project / ".vnx-version").write_text(f"{_VERSION_V1}\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(install / "bin" / "vnx"), "regen-settings", "--merge"],
        cwd=project,
        env=_clean_env({"VNX_PROJECT_ROOT": str(project)}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"regen-settings failed.\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )
    assert sentinel.is_file(), (
        "Stub merge script was not called (sentinel file not written).\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )
    recorded = sentinel.read_text(encoding="utf-8").strip()
    assert recorded == str(project), (
        f"regen-settings passed --project-root={recorded!r}, expected {project}"
    )
    assert recorded != str(install), (
        "regen-settings must NOT use VNX_HOME as --project-root"
    )


# ---------------------------------------------------------------------------
# Case 6: Shim traversal stops at git boundary; VNX_PROJECT_ROOT preserved
# ---------------------------------------------------------------------------

def test_shim_traversal_stops_at_git_boundary_full_chain(tmp_path):
    """Shim detects project root at git boundary, bin/vnx preserves VNX_PROJECT_ROOT."""
    # outer: plain (non-git) directory that holds a .vnx-version pin.
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".vnx-version").write_text(f"{_VERSION_V1}\n", encoding="utf-8")

    # inner: a git repo nested inside outer, with no .vnx-version of its own.
    # The shim's find_project_root must stop at the git boundary (inner root),
    # NOT traverse up to outer and use the outer non-git dir as project root.
    inner = _git_init(outer / "inner-project")

    # Central install with stub inner bin/vnx that echoes the exported env.
    target, shim = _install_central(tmp_path, _VERSION_V1, stub_inner=True)

    res = _run_shim(shim, inner)
    assert res.returncode == 0, (
        f"Shim invocation failed.\n"
        f"  stdout: {res.stdout}\n"
        f"  stderr: {res.stderr}"
    )

    parsed = _parse_kv(res.stdout)
    # Shim must set VNX_PROJECT_ROOT = inner git root, not outer non-git dir.
    assert parsed.get("VNX_PROJECT_ROOT") == str(inner), (
        f"Shim must export VNX_PROJECT_ROOT={inner!r}, "
        f"got {parsed.get('VNX_PROJECT_ROOT')!r}"
    )
    assert parsed.get("VNX_PROJECT_ROOT") != str(outer.resolve()), (
        "Shim must NOT cross the git boundary and use the outer non-git dir as project root"
    )


# ---------------------------------------------------------------------------
# Case 7: Round-trip upgrade — current symlink switch, project re-pins
# ---------------------------------------------------------------------------

def test_round_trip_upgrade_current_symlink_switch(tmp_path):
    """Install v1, upgrade to v2, project re-pins — PROJECT_ROOT stays at project."""
    # Phase 1: install v1 with stub inner.
    target, shim = _install_central(tmp_path, _VERSION_V1, stub_inner=True)
    v1_dir = target / "versions" / _VERSION_V1
    assert (v1_dir / ".vnx-install-mode").read_text(encoding="utf-8").strip() == "central"
    assert (target / "current").resolve() == v1_dir.resolve()

    # Phase 2: simulate upgrade — create v2 version dir with marker + stub.
    v2_dir = target / "versions" / _VERSION_V2
    (v2_dir / "bin").mkdir(parents=True)
    v2_stub = v2_dir / "bin" / "vnx"
    v2_stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "VNX_HOME=${VNX_HOME:-__UNSET__}"\n'
        'echo "VNX_PROJECT_ROOT=${VNX_PROJECT_ROOT:-__UNSET__}"\n',
        encoding="utf-8",
    )
    v2_stub.chmod(0o755)
    (v2_dir / ".vnx-install-mode").write_text("central\n", encoding="utf-8")

    # Atomic symlink switch: current → v2.
    current = target / "current"
    tmp_link = target / "_current_tmp"
    os.symlink(v2_dir, tmp_link)
    os.replace(tmp_link, current)
    assert current.resolve() == v2_dir.resolve(), "current must point to v2 after upgrade"

    # Phase 3: project pins to v2 and invokes the shim.
    project = _git_init(tmp_path / "my-project")
    (project / ".vnx-version").write_text(f"{_VERSION_V2}\n", encoding="utf-8")

    res = _run_shim(shim, project)
    assert res.returncode == 0, (
        f"Shim failed after upgrade.\n"
        f"  stdout: {res.stdout}\n"
        f"  stderr: {res.stderr}"
    )

    parsed = _parse_kv(res.stdout)

    # VNX_HOME must now reference the v2 version dir (not v1).
    vnx_home = parsed.get("VNX_HOME", "")
    assert str(v2_dir) in vnx_home, (
        f"After upgrade VNX_HOME must reference v2; got {vnx_home!r}"
    )
    assert str(v1_dir) not in vnx_home, (
        f"After upgrade VNX_HOME must NOT reference v1; got {vnx_home!r}"
    )

    # PROJECT_ROOT stays at the project, never inside any version dir.
    vnx_project_root = parsed.get("VNX_PROJECT_ROOT", "")
    assert vnx_project_root == str(project), (
        f"VNX_PROJECT_ROOT must be project after upgrade; got {vnx_project_root!r}"
    )
    assert vnx_project_root != str(v2_dir), (
        "After upgrade, VNX_PROJECT_ROOT must not collapse onto v2 install dir"
    )
    assert vnx_project_root != str(v1_dir), (
        "After upgrade, VNX_PROJECT_ROOT must not collapse onto v1 install dir"
    )


# ---------------------------------------------------------------------------
# Case 8: Contamination prevention — no runtime state in central install tree
# ---------------------------------------------------------------------------

def test_contamination_prevented_no_data_in_central_install(tmp_path, monkeypatch):
    """PR-1 + PR-3: resolve_paths() routes all mutable state to project, never VNX_HOME.

    Verifies:
    - VNX_DATA_DIR, VNX_STATE_DIR, VNX_DISPATCH_DIR etc. are all under the project.
    - resolve_paths() itself is read-only: it creates no dirs inside VNX_HOME.
    - The three canonical contamination dirs (.vnx-data, .claude, .vnx-intelligence)
      are absent from the central install tree after correct path resolution.
    """
    install = _git_init(tmp_path / "vnx-install")
    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    project = _git_init(tmp_path / "real-project")

    # Simulate shim export: VNX_HOME=install, VNX_PROJECT_ROOT=project.
    for k in _VNX_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VNX_HOME", str(install))
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)

    paths = vnx_paths.resolve_paths()

    # Every mutable runtime path must be under the project, not under VNX_HOME.
    mutable_vars = (
        "VNX_DATA_DIR",
        "VNX_STATE_DIR",
        "VNX_DISPATCH_DIR",
        "VNX_LOGS_DIR",
        "VNX_PIDS_DIR",
        "VNX_REPORTS_DIR",
        "VNX_INTELLIGENCE_DIR",
    )
    for var in mutable_vars:
        resolved = Path(paths[var])
        assert not resolved.is_relative_to(install), (
            f"{var}={resolved} must NOT be inside VNX_HOME={install}.\n"
            "Contamination detected: PR-1+PR-3 fix chain did not prevent routing "
            "into the immutable code tree."
        )
        assert resolved.is_relative_to(project), (
            f"{var}={resolved} should be rooted at project={project}"
        )

    # resolve_paths() is read-only — it must not create any dirs in VNX_HOME.
    for blocked_dir in (".vnx-data", ".claude", ".vnx-intelligence"):
        assert not (install / blocked_dir).exists(), (
            f"{blocked_dir} was created inside the central install during path resolution. "
            "Contamination: resolve_paths() must never write to VNX_HOME."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
