"""Wave 4 PR-4 — pre-fix contamination detection.

Before the install-central path-resolver fix, a mis-resolved ``PROJECT_ROOT``
could write project runtime state (``.vnx-data``, ``.claude``,
``.vnx-intelligence``) *into* the immutable central code tree
(``~/.vnx-system/versions/<v>/``). After the fix, new invocations write to the
correct project dir, but pre-fix contamination silently persists.

``scripts/vnx_contamination_check.sh`` sweeps a central install for such state
and warns with cleanup instructions — it never deletes. ``vnx doctor`` surfaces
the same detector via ``check_contamination`` (non-fatal WARN).

These tests run the real shell script via subprocess and the real
``check_contamination`` function — neither is reimplemented in the test.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "vnx_contamination_check.sh"
_BLOCKED = (".vnx-data", ".claude", ".vnx-intelligence")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _make_install(tmp_path: Path, *, contaminate: list[str] | None = None) -> Path:
    """A ~/.vnx-system layout with one version dir, optionally contaminated."""
    root = tmp_path / "vnx-system"
    version_dir = root / "versions" / "v1.0.0-rc4"
    version_dir.mkdir(parents=True)
    (version_dir / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    for entry in contaminate or []:
        (version_dir / entry).mkdir(parents=True, exist_ok=True)
    return root


# ── standalone script: --install-root sweep ──────────────────────────────────
def test_install_root_clean_returns_zero(tmp_path):
    root = _make_install(tmp_path)
    result = _run("--install-root", str(root))
    assert result.returncode == 0, result.stderr
    assert "Clean" in result.stdout


def test_install_root_detects_vnx_data(tmp_path):
    root = _make_install(tmp_path, contaminate=[".vnx-data"])
    result = _run("--install-root", str(root))
    assert result.returncode == 1
    assert ".vnx-data" in result.stderr
    assert "rm -rf" in result.stderr  # actionable cleanup instruction
    assert "manually" in result.stderr  # never auto-deletes


def test_install_root_detects_claude_settings(tmp_path):
    root = _make_install(tmp_path, contaminate=[".claude"])
    version_dir = root / "versions" / "v1.0.0-rc4"
    (version_dir / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    result = _run("--install-root", str(root))
    assert result.returncode == 1
    assert ".claude" in result.stderr
    # Detection is non-destructive: the contaminated file is still present.
    assert (version_dir / ".claude" / "settings.json").is_file()


def test_install_root_scans_every_version(tmp_path):
    root = _make_install(tmp_path)
    dirty = root / "versions" / "v0.9.0"
    (dirty / ".vnx-intelligence").mkdir(parents=True)
    result = _run("--install-root", str(root))
    assert result.returncode == 1
    assert "v0.9.0" in result.stderr
    assert ".vnx-intelligence" in result.stderr


# ── standalone script: --version-dir (single dir, used by vnx doctor) ─────────
def test_version_dir_clean(tmp_path):
    root = _make_install(tmp_path)
    version_dir = root / "versions" / "v1.0.0-rc4"
    result = _run("--version-dir", str(version_dir), "--quiet")
    assert result.returncode == 0
    assert result.stdout == ""  # --quiet suppresses the clean message


def test_version_dir_contaminated(tmp_path):
    root = _make_install(tmp_path, contaminate=[".vnx-data"])
    version_dir = root / "versions" / "v1.0.0-rc4"
    result = _run("--version-dir", str(version_dir))
    assert result.returncode == 1
    assert str(version_dir / ".vnx-data") in result.stderr


# ── standalone script: graceful + error paths ────────────────────────────────
def test_missing_install_root_is_clean(tmp_path):
    result = _run("--install-root", str(tmp_path / "nope"))
    assert result.returncode == 0
    assert "No central install" in result.stdout


def test_install_root_without_value_is_usage_error(tmp_path):
    result = _run("--install-root")
    assert result.returncode == 2


def test_unknown_option_is_usage_error(tmp_path):
    result = _run("--bogus")
    assert result.returncode == 2
    assert "Unknown option" in result.stderr


# ── vnx doctor integration: check_contamination ──────────────────────────────
def _load_doctor():
    scripts_dir = _REPO_ROOT / "scripts"
    spec = importlib.util.spec_from_file_location(
        "vnx_doctor_under_test", scripts_dir / "vnx_doctor.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolve field types via
    # sys.modules[cls.__module__], which fails for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_central_vnx_home(tmp_path: Path, *, contaminate: list[str] | None = None) -> Path:
    """A VNX_HOME standing in for ~/.vnx-system/versions/<v> with the real script."""
    home = tmp_path / "versions" / "v1.0.0-rc4"
    (home / "scripts").mkdir(parents=True)
    (home / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    import shutil

    shutil.copy(_SCRIPT, home / "scripts" / "vnx_contamination_check.sh")
    for entry in contaminate or []:
        (home / entry).mkdir(parents=True, exist_ok=True)
    return home


def test_doctor_check_skips_non_central(tmp_path):
    doctor = _load_doctor()
    home = tmp_path / "embedded-project"
    (home / "scripts").mkdir(parents=True)
    # No .vnx-install-mode marker → not a central install → check is a no-op.
    results = doctor.check_contamination({"VNX_HOME": str(home)})
    assert results == []


def test_doctor_check_passes_when_clean(tmp_path):
    doctor = _load_doctor()
    home = _make_central_vnx_home(tmp_path)
    results = doctor.check_contamination({"VNX_HOME": str(home)})
    assert len(results) == 1
    assert results[0].status == doctor.PASS


def test_doctor_check_warns_on_contamination(tmp_path):
    doctor = _load_doctor()
    home = _make_central_vnx_home(tmp_path, contaminate=[".vnx-data"])
    results = doctor.check_contamination({"VNX_HOME": str(home)})
    assert len(results) == 1
    # Non-fatal: contamination is a WARN, never FAIL — must not break doctor exit.
    assert results[0].status == doctor.WARN
    assert results[0].status != doctor.FAIL
    assert any(".vnx-data" in d for d in results[0].details)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
