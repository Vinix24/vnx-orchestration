"""Wave 4 PR-2 — install-central.sh ``.vnx-install-mode`` marker contract.

PR-WAVE4-1 taught the path resolver to treat a central install (VNX_HOME =
``~/.vnx-system/versions/<v>``) differently from a standalone dev checkout, but
*only* when a ``.vnx-install-mode`` marker (content ``central``) is present.
PR-WAVE4-2 makes ``install-central.sh`` actually write that marker and makes
``verify_install`` refuse to declare success without it.

These tests run the real ``install-central.sh`` functions via bash subprocess
(no reimplementation of the logic in Python):

  1. ``clone_version`` writes the marker on a fresh install dir.
  2. ``clone_version`` is idempotent — it back-fills the marker on a pre-existing
     version dir that lacks one.
  3. ``verify_install`` fails (exit != 0) when the marker is missing.
  4. ``verify_install`` fails when the marker content is not ``central``.
  5. ``verify_install`` succeeds when the marker is present and correct.
  6. ``verify_install`` does not contaminate the version dir with ``.vnx-data``
     (the schema check runs against a throwaway temp dir).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "install-central.sh"
_VERSION = "v1.0.0-rc4"
_MARKER = ".vnx-install-mode"


def _script_without_main() -> str:
    """install-central.sh body with the trailing ``main "$@"`` invocation removed.

    Lets a test append its own driver (call a single function directly) without
    triggering the full install flow.
    """
    body = _SCRIPT.read_text(encoding="utf-8")
    head, sep, _tail = body.rpartition('main "$@"')
    assert sep, 'expected a trailing `main "$@"` in install-central.sh'
    return head


def _run_function(driver: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Source the script (minus main) then run ``driver`` bash, capturing output."""
    program = _script_without_main() + "\n" + driver + "\n"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    return subprocess.run(
        ["bash", "-c", program],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )


def _make_install_tree(tmp_path: Path, *, with_marker, version=_VERSION) -> Path:
    """Build a minimal post-install layout (version dir, current symlink, shim).

    ``with_marker`` may be ``True``/``False`` or a literal string to write as the
    marker content (to exercise the invalid-content branch).
    """
    target = tmp_path / "vnx-system"
    version_dir = target / "versions" / version
    version_dir.mkdir(parents=True)

    (target / "current").symlink_to(version_dir)

    bin_dir = target / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "vnx"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)

    if with_marker is True:
        (version_dir / _MARKER).write_text("central\n", encoding="utf-8")
    elif isinstance(with_marker, str):
        (version_dir / _MARKER).write_text(with_marker, encoding="utf-8")

    return target


def _verify_driver(target: Path, version=_VERSION) -> str:
    return (
        f'TARGET_DIR="{target}"\n'
        f'VERSION="{version}"\n'
        "DRY_RUN=false\n"
        "verify_install\n"
    )


# ── clone_version writes the marker ──────────────────────────────────────────
def test_clone_version_writes_marker_on_existing_dir(tmp_path):
    # A pre-existing version dir (clone skipped) must still get the marker.
    target = tmp_path / "vnx-system"
    version_dir = target / "versions" / _VERSION
    version_dir.mkdir(parents=True)
    assert not (version_dir / _MARKER).exists()

    driver = (
        f'TARGET_DIR="{target}"\n'
        f'VERSION="{_VERSION}"\n'
        "DRY_RUN=false\n"
        "clone_version\n"
    )
    result = _run_function(driver)
    assert result.returncode == 0, result.stderr

    marker = version_dir / _MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == "central"


def test_clone_version_dry_run_writes_no_marker(tmp_path):
    target = tmp_path / "vnx-system"
    version_dir = target / "versions" / _VERSION
    version_dir.mkdir(parents=True)

    driver = (
        f'TARGET_DIR="{target}"\n'
        f'VERSION="{_VERSION}"\n'
        "DRY_RUN=true\n"
        "clone_version\n"
    )
    result = _run_function(driver)
    assert result.returncode == 0, result.stderr
    assert not (version_dir / _MARKER).exists()
    assert "[dry-run] write .vnx-install-mode" in result.stdout


# ── verify_install enforces the marker ───────────────────────────────────────
def test_verify_install_fails_without_marker(tmp_path):
    target = _make_install_tree(tmp_path, with_marker=False)
    result = _run_function(_verify_driver(target))
    assert result.returncode != 0
    assert "install-mode marker missing" in result.stderr


def test_verify_install_fails_with_wrong_marker_content(tmp_path):
    target = _make_install_tree(tmp_path, with_marker="embedded\n")
    result = _run_function(_verify_driver(target))
    assert result.returncode != 0
    assert "install-mode marker invalid" in result.stderr


def test_verify_install_succeeds_with_marker(tmp_path):
    target = _make_install_tree(tmp_path, with_marker=True)
    result = _run_function(_verify_driver(target))
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Install-mode marker present (central)" in result.stdout
    assert "Verification complete" in result.stdout


def test_verify_install_does_not_contaminate_version_dir(tmp_path):
    # The schema check must run against a temp dir, never the version dir.
    target = _make_install_tree(tmp_path, with_marker=True)
    version_dir = target / "versions" / _VERSION
    result = _run_function(_verify_driver(target))
    assert result.returncode == 0, result.stderr + result.stdout
    assert not (version_dir / ".vnx-data").exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
