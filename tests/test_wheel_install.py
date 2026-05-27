"""Integration test: fresh-venv wheel-install smoke (PR-PIP-1 acceptance gate).

Wraps ``scripts/test_wheel_install.sh`` so the packaging smoke runs as part of
the pytest suite (the suite is what the existing CI workflows execute). The
shell script is the source of truth for the assertions; this test only drives
it and surfaces its output on failure.

Marked ``integration`` (matches ``test_wheel_build.py``) — it builds a wheel and
spins up a virtualenv, so it is slower and network-touching on a cold cache.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "test_wheel_install.sh"

# Preflight exit code emitted by the smoke script when the build toolchain or a
# compatible interpreter is unavailable — treated as skip, not failure.
PREFLIGHT_EXIT = 2


def _build_toolchain_available() -> bool:
    try:
        import build  # noqa: F401
        import setuptools  # noqa: F401
        import wheel  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.integration
def test_wheel_install_smoke():
    """Build the wheel, install into a clean venv, and assert it is a functional engine."""
    if not SMOKE_SCRIPT.is_file():
        pytest.fail(f"smoke script missing: {SMOKE_SCRIPT}")
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if not _build_toolchain_available():
        pytest.skip("build toolchain (build/setuptools/wheel) not installed")

    result = subprocess.run(
        ["bash", str(SMOKE_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=600,
        env={**_smoke_env(), "SMOKE_PYTHON": sys.executable},
    )

    if result.returncode == PREFLIGHT_EXIT:
        pytest.skip(f"smoke preflight failed (env-specific):\n{result.stdout}\n{result.stderr}")

    assert result.returncode == 0, (
        f"wheel-install smoke failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "smoke PASSED" in result.stdout


def _smoke_env() -> dict:
    import os

    # Pass through the real environment; the script sanitizes VNX_*/PYTHONPATH
    # itself for the venv invocations.
    return dict(os.environ)
