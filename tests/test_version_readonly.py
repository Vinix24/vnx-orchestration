"""Test read-only enforcement for pinned version directories.

The vnx_version_ro module makes pinned version directories (vX.Y.Z)
read-only after install/update while leaving ``edge`` writable.  These
tests exercise the Python utility directly and the install-central.sh
bash integration.

Tests are written to fail against the current code before the
vnx_version_ro module and install-central.sh changes are applied.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Late import: the module lives in scripts/lib/, not on the default
# PYTHONPATH for tests.  pytest's conftest.py adds scripts/lib/ to
# sys.path so the import works in test runs.
from vnx_version_ro import (
    is_readonly,
    make_readonly,
    make_writable,
    writeable_version_dir,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _assert_writable(path: Path) -> None:
    """Assert the user-write bit is SET on *path*."""
    mode = path.stat().st_mode
    assert mode & stat.S_IWUSR, f"expected {path} to be user-writable"


def _assert_readonly(path: Path) -> None:
    """Assert the user-write bit is CLEARED on *path*."""
    mode = path.stat().st_mode
    assert not (mode & stat.S_IWUSR), f"expected {path} to be read-only"


def _write_file_should_fail(path: Path, filename: str = "test_write.txt") -> None:
    """Attempt to create a file in *path*; must raise PermissionError."""
    target = path / filename
    with pytest.raises(PermissionError):
        target.write_text("should not be able to write")


def _write_file_should_succeed(path: Path, filename: str = "test_write.txt") -> None:
    """Create a file in *path*; must succeed (dir is writable)."""
    target = path / filename
    target.write_text("writable")
    target.unlink()  # cleanup


# ── test 1: pinned version dir is read-only after make_readonly ──────────────

def test_pinned_version_not_writable_after_lock(tmp_path: Path) -> None:
    """After make_readonly, a pinned version dir rejects writes."""
    version_dir = tmp_path / "v1.3.1"
    version_dir.mkdir()
    # Prove it starts writable
    _write_file_should_succeed(version_dir, "before.txt")
    assert not is_readonly(version_dir)

    make_readonly(version_dir)

    assert is_readonly(version_dir)
    _write_file_should_fail(version_dir, "after.txt")


def test_make_readonly_recursive(tmp_path: Path) -> None:
    """make_readonly removes write bits from files AND subdirectories."""
    version_dir = tmp_path / "v1.0.0"
    scripts_dir = version_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "tool.py"
    script.write_text("print('hello')")

    make_readonly(version_dir)

    _assert_readonly(version_dir)
    _assert_readonly(scripts_dir)
    _assert_readonly(script)
    # Files inside cannot be overwritten
    with pytest.raises(PermissionError):
        script.write_text("would overwrite")


def test_make_writable_restores_write_bits(tmp_path: Path) -> None:
    """make_writable restores the user-write bit after make_readonly."""
    version_dir = tmp_path / "v2.0.0"
    version_dir.mkdir()

    make_readonly(version_dir)
    assert is_readonly(version_dir)

    make_writable(version_dir)
    assert not is_readonly(version_dir)
    _write_file_should_succeed(version_dir)


# ── test 2: edge stays writable ──────────────────────────────────────────────

def test_edge_never_readonly(tmp_path: Path) -> None:
    """edge is always writable; make_readonly and is_readonly are no-ops."""
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()

    assert not is_readonly(edge_dir)

    make_readonly(edge_dir)
    assert not is_readonly(edge_dir)
    _write_file_should_succeed(edge_dir, "still_writable.txt")


def test_edge_ignored_by_make_writable(tmp_path: Path) -> None:
    """make_writable is a no-op for edge (nothing to restore)."""
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()

    # No error, no change
    make_writable(edge_dir)
    assert not is_readonly(edge_dir)


# ── test 3: install flow can write and leaves dir read-only ──────────────────

def test_context_manager_unlocks_and_relocks(tmp_path: Path) -> None:
    """writeable_version_dir unlocks for writes, re-locks on exit."""
    version_dir = tmp_path / "v1.3.1"
    version_dir.mkdir()
    make_readonly(version_dir)
    assert is_readonly(version_dir)

    with writeable_version_dir(version_dir):
        # Inside the context the dir must be writable
        assert not is_readonly(version_dir)
        _write_file_should_succeed(version_dir, "during_install.txt")

    # After the context the dir must be read-only again
    assert is_readonly(version_dir)
    _write_file_should_fail(version_dir, "after_install.txt")


def test_context_manager_nested_is_idempotent(tmp_path: Path) -> None:
    """Nested writeable_version_dir calls don't double-unlock or leak."""
    version_dir = tmp_path / "v1.0.0"
    version_dir.mkdir()
    make_readonly(version_dir)

    with writeable_version_dir(version_dir):
        assert not is_readonly(version_dir)
        with writeable_version_dir(version_dir):  # nested: already writable
            assert not is_readonly(version_dir)
            _write_file_should_succeed(version_dir, "nested.txt")
        # Inner exit: dir was writable on entry, stays writable
        assert not is_readonly(version_dir)
    # Outer exit: dir was read-only on entry, re-locked
    assert is_readonly(version_dir)


def test_context_manager_noop_when_already_writable(tmp_path: Path) -> None:
    """When the dir is already writable, the context manager is a no-op."""
    version_dir = tmp_path / "v1.2.0"
    version_dir.mkdir()
    # No make_readonly — dir is writable from creation
    assert not is_readonly(version_dir)

    with writeable_version_dir(version_dir):
        _write_file_should_succeed(version_dir, "writable.txt")

    # Was writable on entry → stays writable (not locked)
    assert not is_readonly(version_dir)


# ── test 4: error mid-install leaves dir not writable (finally block) ────────

def test_context_manager_relocks_on_exception(tmp_path: Path) -> None:
    """When an exception occurs inside the context, the finally block re-locks."""
    version_dir = tmp_path / "v1.3.0"
    version_dir.mkdir()
    make_readonly(version_dir)
    assert is_readonly(version_dir)

    with pytest.raises(RuntimeError, match="simulated failure"):
        with writeable_version_dir(version_dir):
            assert not is_readonly(version_dir)
            _write_file_should_succeed(version_dir, "before_error.txt")
            raise RuntimeError("simulated failure mid-install")

    # finally block must have re-locked
    assert is_readonly(version_dir)
    _write_file_should_fail(version_dir, "after_error.txt")


def test_context_manager_relocks_on_os_error(tmp_path: Path) -> None:
    """Even on OSError (e.g. disk full), the finally block re-locks."""
    version_dir = tmp_path / "v1.3.1"
    version_dir.mkdir()
    make_readonly(version_dir)

    try:
        with writeable_version_dir(version_dir):
            # Simulate any kind of error that aborts the write
            raise OSError("disk full during write")
    except OSError:
        pass

    assert is_readonly(version_dir)


# ── edge cases ───────────────────────────────────────────────────────────────

def test_is_readonly_non_existent(tmp_path: Path) -> None:
    """is_readonly returns False for non-existent paths."""
    assert not is_readonly(tmp_path / "does_not_exist")


def test_is_readonly_file_not_dir(tmp_path: Path) -> None:
    """is_readonly returns False for regular files."""
    f = tmp_path / "some_file.txt"
    f.write_text("not a dir")
    assert not is_readonly(f)


def test_make_readonly_non_existent_no_error(tmp_path: Path) -> None:
    """make_readonly on a non-existent path does not raise."""
    make_readonly(tmp_path / "nope" / "v9.9.9")
    # No exception = pass


# ── install-central.sh integration ───────────────────────────────────────────

_INSTALL_CENTRAL = _REPO_ROOT / "install-central.sh"


def _run_install_central_driver(driver: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Source install-central.sh (minus main) and run *driver* bash."""
    body = _INSTALL_CENTRAL.read_text(encoding="utf-8")
    head, sep, tail = body.rpartition('main "$@"')
    assert sep, 'expected trailing main "$@"'
    program = head + "\n" + driver + "\n"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    return subprocess.run(
        ["bash", "-c", program],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )


def test_clone_version_locks_after_fresh_clone(tmp_path: Path) -> None:
    """After a fresh clone, clone_version locks the version dir."""
    target = tmp_path / "vnx-system"
    version = "v9.9.9-test-readonly"

    # Use a local git repo as source so we don't hit the network.
    # Create a minimal git repo with a tag.
    source = tmp_path / "source-repo"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True, capture_output=True)
    (source / "VERSION").write_text("9.9.9\n")
    (source / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "tag", version], check=True, capture_output=True)
    # Remove the tag so clone can use it as a branch (--branch needs a branch or tag)
    # git clone --branch works with tags too, so this should be fine.

    driver = (
        f'TARGET_DIR="{target}"\n'
        f'VERSION="{version}"\n'
        f'SOURCE_URL="{source}"\n'
        "DRY_RUN=false\n"
        "clone_version\n"
    )
    result = _run_install_central_driver(driver)
    assert result.returncode == 0, f"clone_version failed:\n{result.stderr}\n{result.stdout}"

    version_dir = target / "versions" / version
    assert version_dir.is_dir(), f"version dir not created at {version_dir}"
    _assert_readonly(version_dir)
    # The marker file must still be readable
    marker = version_dir / ".vnx-install-mode"
    assert marker.is_file()
    assert "central" in marker.read_text(encoding="utf-8")


def test_clone_version_locks_after_existing_dir(tmp_path: Path) -> None:
    """When the dir already exists (idempotent run), clone_version re-locks."""
    target = tmp_path / "vnx-system"
    version = "v9.9.9-test-idem"

    # Create a minimal version dir that already exists (simulating prior locked state)
    version_dir = target / "versions" / version
    version_dir.mkdir(parents=True)
    (version_dir / "VERSION").write_text("9.9.9\n")
    make_readonly(version_dir)
    assert is_readonly(version_dir)

    # Run clone_version on the existing (locked) dir — must unlock, write
    # marker, and re-lock.
    driver = (
        f'TARGET_DIR="{target}"\n'
        f'VERSION="{version}"\n'
        "DRY_RUN=false\n"
        "clone_version\n"
    )
    result = _run_install_central_driver(driver)
    assert result.returncode == 0, f"clone_version failed:\n{result.stderr}\n{result.stdout}"

    assert is_readonly(version_dir)
    marker = version_dir / ".vnx-install-mode"
    assert marker.is_file()
    assert "central" in marker.read_text(encoding="utf-8")


def test_clone_version_skips_lock_for_edge(tmp_path: Path) -> None:
    """clone_version does NOT lock the edge version dir.

    Exercises the existing-dir (idempotent) branch of clone_version since
    a fresh clone of 'edge' requires the source repo to have an 'edge' branch
    (clone_version passes --branch $VERSION directly, not main→edge mapped).
    The lock decision is in lock_version_dir, which is the unit under test.
    """
    target = tmp_path / "vnx-system"
    version = "edge"
    version_dir = target / "versions" / version
    version_dir.mkdir(parents=True)
    (version_dir / "VERSION").write_text("edge\n")

    driver = (
        f'TARGET_DIR="{target}"\n'
        f'VERSION="{version}"\n'
        "DRY_RUN=false\n"
        "clone_version\n"
    )
    result = _run_install_central_driver(driver)
    assert result.returncode == 0, f"clone_version failed:\n{result.stderr}\n{result.stdout}"

    assert version_dir.is_dir()
    _assert_writable(version_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
