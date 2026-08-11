"""Wave 4 PR-4 — regen-settings writes to the project, never the central install.

``vnx regen-settings`` renders ``$PROJECT_ROOT/.claude/settings.json``. In a
central install (VNX_HOME = ``~/.vnx-system/versions/<v>``, immutable shared
code), the only way that target could land inside VNX_HOME is if PROJECT_ROOT
mis-resolved onto VNX_HOME (the Wave 4 install-central bug). PR-WAVE4-4 adds a
marker-gated guard to ``cmd_regen_settings`` that refuses ``--merge`` / ``--full``
in that case.

These tests drive the real ``bin/vnx`` end-to-end (no reimplementation):

  * from a real project dir → settings.json lands in the project, not VNX_HOME.
  * from inside the central install (PROJECT_ROOT collapses onto VNX_HOME) →
    guard error, exit non-zero, nothing written into the code tree.
  * ``--validate`` is read-only and exempt from the guard.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


def _clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _VNX_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def _make_central_install(tmp_path: Path) -> Path:
    """A standalone git repo standing in for ~/.vnx-system/versions/<v>.

    Ships exactly the pieces ``vnx regen-settings`` touches: the CLI, the path
    resolver, the regen command, the merge engine, and the settings template.
    """
    install = _git_init(tmp_path / "vnx-install")

    (install / "bin").mkdir()
    shutil.copy(_REPO_ROOT / "bin" / "vnx", install / "bin" / "vnx")
    os.chmod(install / "bin" / "vnx", 0o755)

    (install / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "lib" / "vnx_paths.sh",
        install / "scripts" / "lib" / "vnx_paths.sh",
    )

    (install / "scripts" / "commands").mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "commands" / "regen_settings.sh",
        install / "scripts" / "commands" / "regen_settings.sh",
    )
    shutil.copy(
        _REPO_ROOT / "scripts" / "vnx_settings_merge.py",
        install / "scripts" / "vnx_settings_merge.py",
    )

    (install / "templates").mkdir()
    shutil.copy(
        _REPO_ROOT / "templates" / "settings_vnx_keys.json.tmpl",
        install / "templates" / "settings_vnx_keys.json.tmpl",
    )

    (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    return install


def _run_vnx(install: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(install / "bin" / "vnx"), *args],
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )


# ── target lands in the project, not the central install ──────────────────────
def test_regen_full_writes_into_project_not_central(tmp_path):
    install = _make_central_install(tmp_path)
    project = _git_init(tmp_path / "real-project")

    result = _run_vnx(install, project, "regen-settings", "--full", "--no-backup")
    assert result.returncode == 0, result.stderr + result.stdout

    project_settings = project / ".claude" / "settings.json"
    assert project_settings.is_file(), "settings.json must be created in the project"
    # The central code tree must remain untouched.
    assert not (install / ".claude").exists()

    data = json.loads(project_settings.read_text(encoding="utf-8"))
    assert "hooks" in data and "permissions" in data


def test_regen_merge_writes_into_project_not_central(tmp_path):
    install = _make_central_install(tmp_path)
    project = _git_init(tmp_path / "real-project")

    result = _run_vnx(install, project, "regen-settings", "--merge", "--no-backup")
    assert result.returncode == 0, result.stderr + result.stdout
    assert (project / ".claude" / "settings.json").is_file()
    assert not (install / ".claude").exists()
    assert _GUARD_MSG not in result.stderr


# ── guarantee: never write into VNX_HOME when PROJECT_ROOT collapses onto it ──
# Defense in depth — running from inside the central install collapses
# PROJECT_ROOT onto VNX_HOME. The PR-3 source-time guard in vnx_paths.sh and the
# PR-4 regen guard both target this; the observable guarantee is identical: the
# command fails and writes nothing into the immutable code tree.
def test_regen_merge_blocked_from_inside_central_install(tmp_path):
    install = _make_central_install(tmp_path)
    result = _run_vnx(install, install, "regen-settings", "--merge", "--no-backup")
    assert result.returncode != 0
    assert _GUARD_MSG in result.stderr
    assert not (install / ".claude").exists()


def test_regen_full_blocked_from_inside_central_install(tmp_path):
    install = _make_central_install(tmp_path)
    result = _run_vnx(install, install, "regen-settings", "--full", "--no-backup")
    assert result.returncode != 0
    assert _GUARD_MSG in result.stderr
    assert not (install / ".claude").exists()


# ── unit: the PR-4 regen guard's own control flow ─────────────────────────────
# Sources the REAL cmd_regen_settings (not reimplemented) with a spy standing in
# for the PR-3 _guard_not_vnx_home collaborator, to prove that write modes invoke
# the guard against PROJECT_ROOT and that --validate is exempt. This is the inner
# defense layer that catches PROJECT_ROOT == VNX_HOME even when the source-time
# guard does not fire (e.g. VNX_DATA_DIR explicitly relocated to a real project).
def _run_regen_unit(mode: str, *, guard_returns: int) -> subprocess.CompletedProcess:
    regen = _REPO_ROOT / "scripts" / "commands" / "regen_settings.sh"
    program = f"""
set -u
source "{regen}"
err() {{ printf '%s\\n' "$*" >&2; }}
log() {{ printf '%s\\n' "$*"; }}
PROJECT_ROOT="/collapsed/vnx-home"
VNX_HOME="/collapsed/vnx-home"
_guard_not_vnx_home() {{ printf 'GUARD_CALLED:%s\\n' "$1" >&2; return {guard_returns}; }}
cmd_regen_settings {mode} --no-backup
printf 'RC=%s\\n' "$?"
"""
    return subprocess.run(
        ["bash", "-c", program], capture_output=True, text=True, env=_clean_env()
    )


@pytest.mark.parametrize("mode", ["--merge", "--full"])
def test_regen_guard_invoked_for_write_modes(mode):
    result = _run_regen_unit(mode, guard_returns=1)
    assert "GUARD_CALLED:/collapsed/vnx-home" in result.stderr
    assert "RC=1" in result.stdout  # guard tripping aborts the command


def test_regen_guard_skipped_for_validate():
    result = _run_regen_unit("--validate", guard_returns=1)
    # --validate is read-only and must not consult the write guard at all.
    assert "GUARD_CALLED" not in result.stderr


# ── OI-1139: a fresh install must not ship dead hook pins ─────────────────────
# install.sh's own install-time placeholder substitution (_substitute_install_
# templates) scans every shipped .py file for the literal string "{{VNX_HOME}}"
# and rewrites it in place. vnx_settings_merge.py used that exact spelling as
# its OWN (deferred, merge-time) search pattern for rendering
# settings_vnx_keys.json.tmpl — so a real `install.sh` run clobbered the
# search-pattern string literal inside the *installed copy* of
# vnx_settings_merge.py, turning its .replace() call into a silent no-op. The
# real placeholder in the template (protected only by its .tmpl extension)
# then survived un-substituted into settings.json, and hookpin_check's path-
# token regex (which recognizes /, ~, $VAR but not {{...}}) picked up the
# "/scripts/hooks/....sh" tail as a bare, filesystem-root-relative path,
# reporting it as a dead pin.
#
# This drives the REAL install.sh against a REAL fresh target directory, then
# the REAL (installed) vnx_settings_merge.py --full — no reimplementation of
# either — and asserts every rendered hook command resolves to a real file,
# exactly mirroring the "vnx doctor smoke" CI job.
def test_fresh_install_render_produces_no_dead_hook_pins(tmp_path):
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    import hookpin_check  # noqa: E402  (local import — path only needed here)

    target = tmp_path / "fresh-project"
    target.mkdir()
    install_result = subprocess.run(
        ["bash", str(_REPO_ROOT / "install.sh"), str(target)],
        capture_output=True, text=True, env=_clean_env(),
    )
    assert install_result.returncode == 0, (
        "install.sh failed:\n" + install_result.stdout + install_result.stderr
    )

    vnx_home = target / ".vnx"
    merge_script = vnx_home / "scripts" / "vnx_settings_merge.py"
    assert merge_script.is_file(), f"install.sh did not ship {merge_script}"

    # Mirror scripts/vnx_init.py::bootstrap_hooks(), which deploys
    # sessionstart.sh into the project BEFORE triggering the settings render —
    # its {{PROJECT_ROOT}}-relative pin only resolves once this file exists.
    hooks_dir = target / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(vnx_home / "hooks" / "sessionstart.sh"), str(hooks_dir / "sessionstart.sh"))

    render_result = subprocess.run(
        [sys.executable, str(merge_script),
         "--full", "--project-root", str(target), "--vnx-home", str(vnx_home),
         "--no-backup"],
        capture_output=True, text=True, env=_clean_env(),
    )
    assert render_result.returncode == 0, (
        "vnx_settings_merge.py --full failed against a fresh install layout:\n"
        + render_result.stdout + render_result.stderr
    )

    settings_path = target / ".claude" / "settings.json"
    assert settings_path.is_file(), "settings.json was not created"
    raw_settings = settings_path.read_text(encoding="utf-8")
    assert "{{" not in raw_settings, (
        "settings.json contains an unrendered template placeholder — the "
        "install-root substitution silently failed:\n" + raw_settings
    )

    findings = hookpin_check.check_project_hook_pins(target)
    assert findings, "expected at least one configured hook pin to check"
    dead = [f for f in findings if f.status == hookpin_check.STATUS_MISSING]
    assert not dead, "dead hook pin(s) after a fresh install render:\n" + "\n".join(
        f"  {f.event} ({f.matcher}): {f.raw_path} -> {f.resolved_path}" for f in dead
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
